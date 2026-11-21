"""
services.py - the "brain" of MediKiosk.

Everything here is plain Python (no Flask routes):
  * time helpers (minutes <-> "9:15 AM")
  * AI ward triage (calls your n8n workflow, falls back to keyword rules)
  * slot finding / doctor assignment (the AUTO-TIMER that fixes Consulting -> Free)
  * live doctor status + queue maths
"""
import json
import os
import math
import re
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

WARDS = ["General Ward", "Emergency Ward", "Mental Health Ward"]


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def _load_timezone():
    """Hospital time zone. Servers (Render etc.) run on UTC, so the site must not trust the server clock.
    Default Asia/Kolkata (India). Change with APP_TIMEZONE=<zone>, e.g. Asia/Dubai; empty = server time."""
    name = os.environ.get("APP_TIMEZONE", "Asia/Kolkata").strip()
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception as exc:   # unknown zone name or no tz database
        print(f"[time] could not load time zone '{name}': {exc} - using the server clock")
        return None


TZ = _load_timezone()


def now_local():
    """Current hospital time as a plain (naive) datetime."""
    return datetime.now(TZ).replace(tzinfo=None) if TZ else datetime.now()


def today_iso():
    return now_local().date().isoformat()


def utc_to_local_str(value):
    """Database timestamps (CURRENT_TIMESTAMP) are UTC: show them in hospital time."""
    if not value or not isinstance(value, str):
        return value
    try:
        dt = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return value


def minutes_now():
    n = now_local()
    return n.hour * 60 + n.minute


def fmt_time(minutes):
    """540 -> '9:00 AM'"""
    if minutes is None:
        return "-"
    h, m = divmod(int(minutes), 60)
    h %= 24
    suffix = "AM" if h < 12 else "PM"
    return f"{h % 12 or 12}:{m:02d} {suffix}"


def date_label(iso):
    """'2026-11-12' -> 'Tomorrow' / 'Today' / 'Fri, 13 Nov 2026'"""
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    t = now_local().date()
    if d == t:
        return "Today"
    if d == t + timedelta(days=1):
        return "Tomorrow"
    return d.strftime("%a, %d %b %Y")


def parse_shift(label):
    """'9:00 AM – 1:00 PM' -> (540, 780)"""
    found = re.findall(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])", label or "")
    if len(found) < 2:
        raise ValueError(f"Cannot read shift time: {label!r}")
    out = []
    for h, m, ap in found[:2]:
        h = int(h) % 12 + (12 if ap.upper() == "PM" else 0)
        out.append(h * 60 + int(m))
    start, end = out[0], out[1]
    if end <= start:          # shift runs past midnight (e.g. 12:00 PM - 1:00 AM): bookable until midnight
        end = 24 * 60
    return start, end


def parse_hhmm(value):
    """'08:30' -> 510, or None if invalid."""
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (value or "").strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


# --------------------------------------------------------------------------- #
# Ward triage  (n8n "Message a model" replacement / fallback)
# --------------------------------------------------------------------------- #
EMERGENCY_WORDS = [
    "chest pain", "heart attack", "breath", "breathing", "unconscious", "faint",
    "bleeding", "blood loss", "accident", "fracture", "broken bone", "burn",
    "seizure", "fits", "stroke", "paralys", "poison", "snake bite", "severe pain",
    "choking", "head injury", "suicid", "overdose", "emergency",
]
MENTAL_WORDS = [
    "anxiety", "anxious", "depress", "stress", "panic", "insomnia", "can't sleep",
    "cannot sleep", "sleep problem", "mental", "mood", "hallucinat", "lonely",
    "sad", "overthink", "phobia", "addiction", "counsel", "ocd", "bipolar",
]


def local_triage(symptoms):
    text = (symptoms or "").lower()
    if any(w in text for w in EMERGENCY_WORDS):
        return "Emergency Ward"
    if any(w in text for w in MENTAL_WORDS):
        return "Mental Health Ward"
    return "General Ward"


def normalize_ward(value):
    """Turns any AI answer ('general ward.', 'GENERAL', ...) into an exact ward name."""
    text = str(value or "").lower()
    if "emerg" in text:
        return "Emergency Ward"
    if "mental" in text or "psych" in text:
        return "Mental Health Ward"
    if "general" in text:
        return "General Ward"
    return None


def _extract_ward(payload):
    if isinstance(payload, list) and payload:
        payload = payload[0]
    if isinstance(payload, dict):
        for key in ("ward", "Ward", "ward_name", "output", "text", "message"):
            if key in payload:
                found = normalize_ward(payload[key])
                if found:
                    return found
    return normalize_ward(json.dumps(payload) if not isinstance(payload, str) else payload)


def _post_json(url, data, timeout):
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8") or "{}"
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


class _Status(dict):
    """Status lines shown on the Staff desk. n8n results are also remembered in the database,
    so they are still there after you restart the website."""
    persist = None   # set by app.py

    def __setitem__(self, key, value):
        if key in ("triage", "notify") and self.persist and not value.startswith(("connected", "off")):
            value = f"{value} [{now_local().strftime('%d %b %H:%M')}]"
            dict.__setitem__(self, key, value)
            try:
                self.persist(key, value)
            except Exception as exc:
                print(f"[status] could not save: {exc}")
            return
        dict.__setitem__(self, key, value)


STATUS = _Status({"triage": "not used yet", "notify": "not used yet", "excel": "not written yet",
          "email": "off (set SMTP_* variables to turn on)"})


def env_url(name, default):
    """URL from environment; 'off' disables it; empty -> default."""
    val = os.environ.get(name, "").strip()
    if val.lower() == "off":
        return ""
    return val or default


# --------------------------------------------------------------------------- #
# n8n failover: if an n8n webhook fails, stop calling it for a while (so bookings stay fast) and let the
# website's own functions do the work. After N8N_RETRY_SECONDS it tries n8n again automatically.
# --------------------------------------------------------------------------- #
N8N_RETRY_SECONDS = int(os.environ.get("N8N_RETRY_SECONDS", "120"))
_n8n_down_until = {"triage": 0.0, "notify": 0.0}


def n8n_available(kind):
    return time.time() >= _n8n_down_until.get(kind, 0.0)


def _n8n_failed(kind):
    _n8n_down_until[kind] = time.time() + N8N_RETRY_SECONDS


def _n8n_ok(kind):
    _n8n_down_until[kind] = 0.0


def n8n_visible(configured):
    """Should the Staff desk show n8n details? Only while n8n is configured AND healthy."""
    if not configured:
        return False
    if not (n8n_available("triage") and n8n_available("notify")):
        return False
    return not any(str(STATUS.get(k, "")).startswith("FAILED") for k in ("triage", "notify"))


def triage(symptoms, patient, n8n_url=""):
    """Returns (ward, source). Never raises - the website keeps working if n8n is down."""
    if n8n_url and n8n_available("triage"):
        try:
            payload = {
                "name": patient["name"],
                "age": patient["age"],
                "gender": patient["gender"],
                "symptoms": symptoms,
            }
            ward = _extract_ward(_post_json(n8n_url, payload, timeout=float(os.environ.get("N8N_TRIAGE_TIMEOUT", "8"))))
            if ward:
                STATUS["triage"] = "OK - AI chose " + ward
                _n8n_ok("triage")
                return ward, "n8n AI"
            STATUS["triage"] = "FAILED: n8n answered but no ward found in reply - using built-in rules"
        except Exception as exc:  # network error, timeout, bad JSON ...
            STATUS["triage"] = f"FAILED: {exc} - using built-in rules (is the n8n workflow Published?)"
            _n8n_failed("triage")
            print(f"[triage] n8n call failed, using built-in rules: {exc}")
    return local_triage(symptoms), "Keyword rules"


def notify_n8n_async(url, data, fallback=None):
    """Fire-and-forget POST to n8n (emails / Google Sheet). Booking never waits for it.
    If n8n fails, `fallback(data)` (the website's own e-mails) runs instead."""
    if not url:
        return

    def _send():
        try:
            _post_json(url, data, timeout=float(os.environ.get("N8N_NOTIFY_TIMEOUT", "15")))
            STATUS["notify"] = "OK - last booking sent to n8n"
            _n8n_ok("notify")
        except Exception as exc:
            STATUS["notify"] = f"FAILED: {exc} (is the n8n workflow Published?)"
            _n8n_failed("notify")
            print(f"[notify] n8n notify failed: {exc}")
            if fallback:
                try:
                    fallback(data)
                except Exception as exc2:
                    print(f"[notify] fallback e-mail failed: {exc2}")

    threading.Thread(target=_send, daemon=True).start()


# --------------------------------------------------------------------------- #
# AUTO-TIMER + scheduling
# --------------------------------------------------------------------------- #
def auto_complete(db):
    """Any booked appointment whose slot has ended is marked Completed automatically.
    This is what turns a doctor from 'Consulting' back to 'Free' without anyone
    editing a sheet."""
    t, m = today_iso(), minutes_now()
    db.execute(
        """UPDATE appointments
              SET status = 'Completed', completed_at = ?
            WHERE status = 'Booked'
              AND (appt_date < ? OR (appt_date = ? AND end_min <= ?))""",
        (now_local().isoformat(timespec="seconds"), t, t, m),
    )
    db.commit()


def find_slot(db, doctor, day_iso):
    """Earliest free start minute for this doctor on that day, or None."""
    if doctor["on_leave"]:
        return None
    dur = doctor["consult_minutes"]
    cand = doctor["shift_start"]
    if day_iso == today_iso():
        cand = max(cand, int(math.ceil(minutes_now() / 5.0)) * 5)
    elif day_iso < today_iso():
        return None
    booked = db.execute(
        """SELECT start_min, end_min FROM appointments
            WHERE doctor_id = ? AND appt_date = ? AND status = 'Booked'
            ORDER BY start_min""",
        (doctor["id"], day_iso),
    ).fetchall()
    for b in booked:
        if b["end_min"] <= cand:
            continue
        if cand + dur <= b["start_min"]:
            break
        cand = max(cand, b["end_min"])
    if cand + dur <= doctor["shift_end"]:
        return cand
    return None


PRIORITY_WARDS = ("Emergency Ward", "Mental Health Ward")


def _load_today(db, doc_id, day):
    return db.execute(
        "SELECT COUNT(*) FROM appointments WHERE doctor_id=? AND appt_date=? AND status='Booked'",
        (doc_id, day),
    ).fetchone()[0]


def choose_priority_doctor(db, ward):
    """Emergency and Mental Health patients are critical: give them a doctor who is
    on duty RIGHT NOW. Preference: same ward -> Emergency Ward -> any doctor on duty.
    Returns (doctor, today, start_min) or None if nobody is on duty."""
    t, m = today_iso(), minutes_now()
    on_duty = [d for d in db.execute("SELECT * FROM doctors WHERE on_leave = 0").fetchall()
               if d["shift_start"] <= m < d["shift_end"]]
    if not on_duty:
        return None

    def rank(d):
        if d["ward"] == ward:
            pref = 0
        elif d["ward"] == "Emergency Ward":
            pref = 1
        else:
            pref = 2
        return (pref, _load_today(db, d["id"], t), d["id"])

    doc = sorted(on_duty, key=rank)[0]
    return doc, t, m


def push_queue_back(db, doctor, day, start_min):
    """A priority patient jumps the queue: everyone still WAITING for this doctor
    moves back by one consultation slot."""
    dur = doctor["consult_minutes"]
    db.execute(
        """UPDATE appointments SET start_min = start_min + ?, end_min = end_min + ?
            WHERE doctor_id = ? AND appt_date = ? AND status = 'Booked' AND start_min >= ?""",
        (dur, dur, doctor["id"], day, start_min),
    )


def choose_doctor(db, ward, day_iso, max_days=7):
    """Pick (doctor, date, start_min, is_priority).
    Emergency + Mental Health: immediate treatment today with an on-duty doctor.
    General Ward (or no doctor on duty): earliest free slot of that ward."""
    if ward in PRIORITY_WARDS:
        pick = choose_priority_doctor(db, ward)
        if pick:
            doc, d, start = pick
            return doc, d, start, True
    emergency = ward == "Emergency Ward"
    start_day = date.fromisoformat(day_iso)
    for offset in range(max_days):
        d = (start_day + timedelta(days=offset)).isoformat()
        if emergency:
            doctors = db.execute("SELECT * FROM doctors WHERE on_leave = 0").fetchall()
        else:
            doctors = db.execute(
                "SELECT * FROM doctors WHERE on_leave = 0 AND ward = ?", (ward,)
            ).fetchall()
        options = []
        for doc in doctors:
            slot = find_slot(db, doc, d)
            if slot is None:
                continue
            same_ward = 0 if doc["ward"] == ward else 1
            options.append((slot, same_ward, _load_today(db, doc["id"], d), doc["id"], doc))
        if options:
            options.sort(key=lambda o: o[:4])
            slot, _, _, _, doc = options[0]
            return doc, d, slot, False
    return None


# --------------------------------------------------------------------------- #
# Tablet adherence (how many doses the patient really took)
# --------------------------------------------------------------------------- #
def adherence_rating(pct):
    if pct is None:
        return "No data"
    if pct >= 80:
        return "Good"
    if pct >= 50:
        return "Fair"
    return "Poor"


def adherence(db, patient_id, days=30):
    """Percentage of scheduled doses marked 'Taken' in the last `days` days.
    A dose not marked within 2 hours of its time counts as Missed."""
    today = date.fromisoformat(today_iso())
    m = minutes_now()
    since = today - timedelta(days=days - 1)
    logs = {
        (r["medication_id"], r["log_date"], r["time_slot"]): r["status"]
        for r in db.execute(
            """SELECT l.* FROM med_logs l JOIN medications md ON md.id = l.medication_id
                WHERE md.patient_id = ? AND l.log_date >= ?""",
            (patient_id, since.isoformat()),
        )
    }
    per_med, tot = [], {"taken": 0, "skipped": 0, "missed": 0}
    for med in db.execute("SELECT * FROM medications WHERE patient_id = ? ORDER BY name", (patient_id,)):
        try:
            start = max(date.fromisoformat(med["start_date"]), since)
        except (TypeError, ValueError):
            start = since
        end = today
        if med["end_date"]:
            try:
                end = min(end, date.fromisoformat(med["end_date"]))
            except ValueError:
                pass
        slots = [x for x in med["times"].split(",") if parse_hhmm(x) is not None]
        c = {"taken": 0, "skipped": 0, "missed": 0}
        d = start
        while d <= end:
            for slot in slots:
                st = logs.get((med["id"], d.isoformat(), slot))
                if st == "Taken":
                    c["taken"] += 1
                elif st == "Skipped":
                    c["skipped"] += 1
                elif med["active"] and (d < today or parse_hhmm(slot) + 120 < m):
                    c["missed"] += 1
            d += timedelta(days=1)
        expected = sum(c.values())
        pct = round(100 * c["taken"] / expected) if expected else None
        for k in tot:
            tot[k] += c[k]
        per_med.append({"id": med["id"], "name": med["name"], "dosage": med["dosage"] or "",
                        "active": med["active"], "expected": expected, "percent": pct,
                        "rating": adherence_rating(pct), **c})
    expected = sum(tot.values())
    pct = round(100 * tot["taken"] / expected) if expected else None
    return {"days": days, "expected": expected, "percent": pct, "rating": adherence_rating(pct),
            "per_med": per_med, **tot}


# --------------------------------------------------------------------------- #
# Live status / queue
# --------------------------------------------------------------------------- #
def doctor_live(db, doc):
    t, m = today_iso(), minutes_now()
    on_shift = doc["shift_start"] <= m < doc["shift_end"]
    current = db.execute(
        """SELECT a.token, a.end_min, p.name AS patient_name
             FROM appointments a JOIN patients p ON p.id = a.patient_id
            WHERE a.doctor_id = ? AND a.appt_date = ? AND a.status = 'Booked'
              AND a.start_min <= ? AND a.end_min > ?
            ORDER BY a.start_min LIMIT 1""",
        (doc["id"], t, m, m),
    ).fetchone()
    waiting = db.execute(
        """SELECT token, start_min FROM appointments
            WHERE doctor_id = ? AND appt_date = ? AND status = 'Booked' AND start_min > ?
            ORDER BY start_min""",
        (doc["id"], t, m),
    ).fetchall()
    if doc["on_leave"]:
        status = "On Leave"
    elif current:
        status = "Consulting"
    elif on_shift:
        status = "Free"
    else:
        status = "Off Duty"
    nxt = find_slot(db, doc, t)
    return {
        "id": doc["id"],
        "name": doc["name"],
        "ward": doc["ward"],
        "room": doc["room"],
        "shift": doc["shift_label"],
        "status": status,
        "now_serving": current["token"] if current else None,
        "busy_until": fmt_time(current["end_min"]) if current else None,
        "waiting_count": len(waiting),
        "waiting_tokens": [w["token"] for w in waiting],
        "next_free": fmt_time(nxt) if nxt is not None else "No slot today",
        "est_wait_min": (nxt - m) if nxt is not None else None,
    }


def appointment_live(db, appt):
    """Queue position + estimated wait for one appointment."""
    t, m = today_iso(), minutes_now()
    ahead = db.execute(
        """SELECT COUNT(*) FROM appointments
            WHERE doctor_id = ? AND appt_date = ? AND status = 'Booked' AND start_min < ?""",
        (appt["doctor_id"], appt["appt_date"], appt["start_min"]),
    ).fetchone()[0]
    if appt["appt_date"] == t:
        # appointments already running count as "ahead" only if not finished
        ahead_now = db.execute(
            """SELECT COUNT(*) FROM appointments
                WHERE doctor_id = ? AND appt_date = ? AND status = 'Booked'
                  AND start_min < ? AND end_min > ?""",
            (appt["doctor_id"], t, appt["start_min"], m),
        ).fetchone()[0]
        ahead = ahead_now
    in_consult = (
        appt["status"] == "Booked"
        and appt["appt_date"] == t
        and appt["start_min"] <= m < appt["end_min"]
    )
    if appt["status"] != "Booked":
        state, wait = appt["status"], None
    elif in_consult:
        state, wait = "In consultation", 0
    elif appt["appt_date"] == t:
        state, wait = "Waiting", max(0, appt["start_min"] - m)
    else:
        state, wait = "Upcoming", None
    if state == "In consultation":
        wait_text = "Your turn now"
    elif wait is not None:
        wait_text = f"{wait} min"
    elif state == "Upcoming":
        wait_text = f"Starts {date_label(appt['appt_date'])}, {fmt_time(appt['start_min'])}"
    else:
        wait_text = "–"
    return {
        "id": appt["id"],
        "token": appt["token"],
        "state": state,
        "wait_text": wait_text,
        "patients_ahead": ahead,
        "wait_min": wait,
        "time": fmt_time(appt["start_min"]),
        "date": date_label(appt["appt_date"]),
    }
