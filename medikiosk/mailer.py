"""
mailer.py - optional e-mail medicine reminders (reach the patient even when the website tab is closed).

Turn on by setting these before `python app.py` (Gmail example - use an App Password, not your normal password):
  set SMTP_HOST=smtp.gmail.com
  set SMTP_PORT=587
  set SMTP_USER=you@gmail.com
  set SMTP_PASS=your-16-letter-app-password
Without them nothing is sent and the website keeps working.
"""
import os
import json
import smtplib
import threading
import time
import urllib.error
import urllib.request
from email.message import EmailMessage

import services as svc


def _brevo_key():
    return os.environ.get("BREVO_API_KEY", "").strip()


def sender_address():
    return (os.environ.get("EMAIL_FROM") or os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or "").strip()


def smtp_configured():
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS"))


def configured():
    """E-mail is available if Brevo (web API, works on free hosts) or Gmail/SMTP is set up."""
    return bool(_brevo_key() and sender_address()) or smtp_configured()


def _send_brevo(to, subject, body):
    payload = {"sender": {"name": "MediKiosk - Neelima Hospital", "email": sender_address()},
               "to": [{"email": to}], "subject": subject, "textContent": body}
    req = urllib.request.Request(
        os.environ.get("BREVO_API_URL", "https://api.brevo.com/v3/smtp/email"),
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"api-key": _brevo_key(), "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode("utf-8", "ignore")
        raise RuntimeError(f"Brevo error {exc.code}: {detail}") from None


def send_mail(to, subject, body):
    if _brevo_key() and sender_address():
        return _send_brevo(to, subject, body)
    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM") or os.environ["SMTP_USER"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=20) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def send_due_reminders(db_path, sender=send_mail):
    """E-mail each tablet reminder once, within 30 minutes after its time, if not yet logged. Returns count."""
    t, m = svc.today_iso(), svc.minutes_now()
    from database import open_target
    db = open_target(db_path)
    sent = 0
    try:
        meds = db.execute(
            """SELECT md.*, p.email AS pemail, p.name AS pname FROM medications md
                 JOIN patients p ON p.id = md.patient_id
                WHERE md.active = 1 AND md.start_date <= ?
                  AND (md.end_date IS NULL OR md.end_date = '' OR md.end_date >= ?)""", (t, t)).fetchall()
        for med in meds:
            for slot in med["times"].split(","):
                mins = svc.parse_hhmm(slot)
                if mins is None or not (0 <= m - mins <= 30):
                    continue
                if db.execute("SELECT 1 FROM med_logs WHERE medication_id = ? AND log_date = ? AND time_slot = ?",
                              (med["id"], t, slot)).fetchone():
                    continue
                if db.execute("SELECT 1 FROM reminder_emails WHERE medication_id = ? AND log_date = ? AND time_slot = ?",
                              (med["id"], t, slot)).fetchone():
                    continue
                body = (f"Hello {med['pname']},\n\nIt is time to take your tablet: {med['name']}"
                        f"{' (' + med['dosage'] + ')' if med['dosage'] else ''} at {svc.fmt_time(mins)}.\n"
                        f"{med['notes'] + chr(10) if med['notes'] else ''}\n"
                        "After taking it, open MediKiosk -> Reminders and tap 'Taken' so your doctor can see your progress.\n\n"
                        "- MediKiosk, Neelima Hospital")
                try:
                    sender(med["pemail"], f"Medicine reminder: {med['name']}", body)
                    db.execute("INSERT INTO reminder_emails (medication_id, log_date, time_slot) VALUES (?, ?, ?)",
                               (med["id"], t, slot))
                    db.commit()
                    svc.STATUS["email"] = f"OK - last reminder sent to {med['pemail']}"
                    sent += 1
                except Exception as exc:
                    svc.STATUS["email"] = f"FAILED: {exc}"
                    print(f"[reminder-email] {exc}")
        # second email when a snooze ends and the dose is still not marked
        snz = db.execute(
            """SELECT s.*, md.name, md.dosage, p.email AS pemail, p.name AS pname FROM med_snooze s
                 JOIN medications md ON md.id = s.medication_id JOIN patients p ON p.id = md.patient_id
                WHERE s.log_date = ? AND md.active = 1""", (t,)).fetchall()
        for sn in snz:
            if not (0 <= m - sn["until_min"] <= 30):
                continue
            key = f"{sn['time_slot']}#{sn['until_min']}"
            if db.execute("SELECT 1 FROM med_logs WHERE medication_id = ? AND log_date = ? AND time_slot = ?",
                          (sn["medication_id"], t, sn["time_slot"])).fetchone():
                continue
            if db.execute("SELECT 1 FROM reminder_emails WHERE medication_id = ? AND log_date = ? AND time_slot = ?",
                          (sn["medication_id"], t, key)).fetchone():
                continue
            body = (f"Hello {sn['pname']},\n\nYou snoozed this reminder. It is time to take {sn['name']}"
                    f"{' (' + sn['dosage'] + ')' if sn['dosage'] else ''} now.\n\n"
                    "Open MediKiosk -> Reminders and tap 'Taken' when done.\n\n- MediKiosk, Neelima Hospital")
            try:
                sender(sn["pemail"], f"Reminder again: {sn['name']}", body)
                db.execute("INSERT INTO reminder_emails (medication_id, log_date, time_slot) VALUES (?, ?, ?)",
                           (sn["medication_id"], t, key))
                db.commit()
                svc.STATUS["email"] = f"OK - last reminder sent to {sn['pemail']}"
                sent += 1
            except Exception as exc:
                svc.STATUS["email"] = f"FAILED: {exc}"
                print(f"[reminder-email] {exc}")
    finally:
        db.close()
    return sent


# ---------------------------------------------------------------------------------------------
# Appointment e-mails sent by the website itself (used when n8n is switched off)
# Patient + doctor like the n8n workflow; optional copy to the receptionist (RECEPTIONIST_EMAIL).
# ---------------------------------------------------------------------------------------------
def _flag_off(name):
    return os.environ.get(name, "").strip().lower() in ("0", "false", "off", "no")


def booking_messages(d):
    """Returns [(to, subject, body), ...] for one booking payload (the same dict n8n receives)."""
    urgent = d.get("priority", "").startswith("URGENT")
    who = d["patient_name"]
    by = d.get("booked_by")
    details = (f"Token: #{d['token']}\nDoctor: {d['doctor_name']}\nWard: {d['ward']}\nRoom: {d['room']}\n"
               f"Date: {d['date']}\nExpected time: {d['time']}\n")
    out = []
    if d.get("patient_email"):
        body = (f"Hello {by or who},\n\nYour appointment at Neelima Hospital is confirmed"
                f"{' for ' + who if by and by != who else ''}.\n\n{details}")
        if urgent:
            body += "\nURGENT CASE: please go to the room above right now. You will be seen immediately.\n"
        body += f"\n{d.get('message', '')}\n\nYou can follow the live queue in MediKiosk -> Live Queue.\n\n- MediKiosk, Neelima Hospital"
        out.append((d["patient_email"], f"Appointment confirmed - token #{d['token']} - {d['date']} {d['time']}", body))
    staff_body = (f"New appointment booked.\n\nPatient: {who}\nPhone: {d.get('patient_phone', '')}\n"
                  f"Problem: {d.get('symptoms', '')}\n{details}Priority: {d.get('priority', 'Normal')}\n\n- MediKiosk")
    staff_subject = f"{'URGENT - ' if urgent else ''}New appointment: {who} ({d['ward']}) {d['date']} {d['time']}"
    if d.get("doctor_email") and not _flag_off("NOTIFY_DOCTORS"):
        out.append((d["doctor_email"], staff_subject, staff_body))
    reception = os.environ.get("RECEPTIONIST_EMAIL", "").strip()
    if reception:
        out.append((reception, staff_subject, staff_body))
    return out


def send_booking_emails(d, sender=None):
    """Send all e-mails for one booking. Returns a list of error texts (empty = all sent)."""
    sender = sender or send_mail
    errors = []
    for to, subject, body in booking_messages(d):
        try:
            sender(to, subject, body)
        except Exception as exc:
            errors.append(f"{to}: {exc}")
            print(f"[booking-email] {to}: {exc}")
    return errors


def notify_booking_async(d):
    """Fire-and-forget, like the n8n call: the booking never waits for e-mail."""
    if not configured():
        svc.STATUS["booking_email"] = "off - e-mail settings missing (Brevo or Gmail SMTP), so no appointment e-mails are sent"
        return

    def run():
        errors = send_booking_emails(d)
        svc.STATUS["booking_email"] = ("OK - last booking was e-mailed" if not errors
                                       else "FAILED: " + "; ".join(errors))

    threading.Thread(target=run, daemon=True).start()


def init_status():
    svc.STATUS["booking_email"] = ("ready - sent by the website" if configured()
                                   else "off - e-mail settings missing (Brevo or Gmail SMTP), so no appointment e-mails are sent")
    svc.STATUS["email"] = ("on - waiting for the next tablet time" if configured()
                           else "off - e-mail settings (BREVO_API_KEY or SMTP_*) not found in this window")


# ---------------------------------------------------------------------------------------------
# Neon-friendly mode: a free Neon database sleeps after 5 minutes without queries and only has ~100 compute
# hours a month. Querying every 15 seconds would keep it awake all month, so with PostgreSQL the website
# loads the reminder times into memory and only touches the database while a reminder is actually due.
# Force on/off with LOW_DB_ACTIVITY=1 / 0.
# ---------------------------------------------------------------------------------------------
_sched = {"items": [], "loaded": 0.0, "dirty": True, "windows": []}
SCHED_REFRESH_SECONDS = 1800


def mark_dirty():
    """Call after a medicine is added, changed or deleted."""
    _sched["dirty"] = True


def note_snooze(until_min):
    _sched["windows"].append((svc.today_iso(), until_min))


def lazy_mode(db_path):
    flag = os.environ.get("LOW_DB_ACTIVITY", "").strip().lower()
    if flag in ("1", "true", "yes"):
        return True
    if flag in ("0", "false", "no"):
        return False
    from pg_compat import is_pg_url
    return is_pg_url(db_path)


def _load_schedule(db_path):
    from database import open_target
    db = open_target(db_path)
    try:
        rows = db.execute("SELECT times, start_date, end_date FROM medications WHERE active = 1").fetchall()
    finally:
        db.close()
    items = []
    for r in rows:
        for slot in (r["times"] or "").split(","):
            mins = svc.parse_hhmm(slot)
            if mins is not None:
                items.append((r["start_date"] or "", r["end_date"] or "", mins))
    _sched.update(items=items, loaded=time.time(), dirty=False)


def anything_due(db_path):
    if _sched["dirty"] or time.time() - _sched["loaded"] > SCHED_REFRESH_SECONDS:
        _load_schedule(db_path)
    t, m = svc.today_iso(), svc.minutes_now()
    for start, end, mins in _sched["items"]:
        if start <= t and (not end or end >= t) and 0 <= m - mins <= 30:
            return True
    _sched["windows"] = [(d, u) for d, u in _sched["windows"] if d == t and m - u <= 30]
    return any(0 <= m - u <= 30 for d, u in _sched["windows"])


def alert_receptionist_async(subject, body):
    """E-mail the receptionist (RECEPTIONIST_EMAIL) about something that needs a human, e.g. no free doctor."""
    to = os.environ.get("RECEPTIONIST_EMAIL", "").strip()
    if not (to and configured()):
        return

    def run():
        try:
            send_mail(to, subject, body)
        except Exception as exc:
            print(f"[alert] {exc}")

    threading.Thread(target=run, daemon=True).start()


def loop(db_path):
    init_status()
    lazy = lazy_mode(db_path)
    while True:
        time.sleep(60 if lazy else 15)      # normal: check often so the e-mail arrives at the tablet time
        if configured():
            try:
                if lazy and not anything_due(db_path):
                    continue
                send_due_reminders(db_path)
            except Exception as exc:
                print(f"[reminder-email] {exc}")
