# MediKiosk - project summary

**Problem:** long, unmanaged waiting in a hospital; an Excel sheet could not show when a doctor became free again.
**Solution:** a booking website that picks the ward, assigns a free doctor in a 15-minute slot, gives each patient a token, room,
time and live queue, and keeps working even when its automation tool fails.

**Flow of one booking:** patient form (up to 3 people) -> triage (n8n AI, or built-in rules) -> doctor + slot (Emergency / Mental Health first)
-> saved in the database -> e-mails to patient, doctor, receptionist + Google Sheet row (n8n, or the website itself) -> ticket with token, room, time, queue.

**Features:** accounts; family booking; live queue; "mark as done"; reports and prescriptions; lab-value checker; tablet reminders with alarm, snooze and e-mail;
weekly adherence score; staff desk; doctor hours editor; OP receipt (print / PDF, 6-day validity); Excel export; automatic n8n failover.

**Stack:** Python Flask, HTML / CSS / JavaScript, SQLite / PostgreSQL (Neon) / MySQL, n8n (local) with a local AI model, Gmail / Brevo, Google Sheets, GitHub, Render.

**Honest limits:** shifts past midnight are bookable only until midnight; the report checker is not a diagnosis; free hosting sleeps and has no persistent disk.
