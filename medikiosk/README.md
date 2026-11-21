# MediKiosk - Neelima Hospital appointment system

Patients sign up, book up to 3 appointments at a time (family), see a token, room, time and live queue, store reports and
get tablet reminders (alarm + e-mail). Staff see every doctor live, change doctor hours, print/download OP receipts and
export Excel. An n8n workflow adds AI ward choice and booking e-mails - and if n8n is missing or fails, the website
does the same jobs by itself, without anyone noticing.

## 1. Quick start on your laptop (Windows PowerShell)
```
cd "C:\path\to\medikiosk"
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
Copy-Item .env.example .env        # then open .env and fill it in
python app.py
```
Open http://127.0.0.1:5000  -  staff desk: http://127.0.0.1:5000/staff/login  (admin / admin123 - change it, see STAFF_PASSWORD).
Everything you would otherwise type as $env:... lines goes into the file `.env` (never share or upload it).
Real environment variables (e.g. on Render) always win over `.env`.

## 2. Settings (.env)
| Setting | Meaning |
|---|---|
| DATABASE_URL | empty = local file. `postgresql://...` (Neon) or `mysql://...` for a hosted database |
| SECRET_KEY | long random text (keeps users logged in) |
| STAFF_USERNAME / STAFF_PASSWORD | staff login - used only the first time the staff account is created |
| USE_N8N | `1` (default) use n8n; `0` run without n8n |
| N8N_TRIAGE_URL / N8N_NOTIFY_URL | n8n webhook addresses, e.g. `http://localhost:5678/webhook/medikiosk-triage` and `.../medikiosk-booked` |
| N8N_TRIAGE_TIMEOUT | seconds to wait for the AI (slow local model: 40) |
| N8N_RETRY_SECONDS | after an n8n failure, try n8n again after this many seconds (default 120) |
| BREVO_API_KEY + EMAIL_FROM | e-mail through Brevo's web API (works on free hosts) |
| SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS | or e-mail through Gmail SMTP (App Password) |
| RECEPTIONIST_EMAIL | gets a copy of every booking and an alert when no doctor is free |
| NOTIFY_DOCTORS | `0` = do not e-mail doctors |
| GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_SHEET_ID / GOOGLE_SHEET_TAB | website logs bookings to your Google Sheet when n8n is off or down (see gsheets.py) |
| APP_TIMEZONE | default Asia/Kolkata (servers run on UTC) |
| DATA_DIR | folder on a persistent disk for uploads and the Excel file (hosting) |
| COOKIE_SECURE | `1` when the site is served over https |

## 3. With n8n, without n8n, and automatic failover
- **With n8n:** n8n picks the ward (AI) and sends the doctor / patient / receptionist e-mails and logs the sheet row.
- **Without n8n** (`USE_N8N=0`): built-in rules pick the ward; the website sends the same e-mails (needs Brevo or SMTP settings)
  and logs the Google Sheet row (needs the GOOGLE_* settings). Doctor assignment, queue, token, reminders are always done by the website.
- **Failover:** if n8n stops, the trial ends, a workflow is unpublished or the AI model is off, the website notices on its own,
  switches to the built-in way for that booking, hides the n8n lines on the Staff desk, and tries n8n again every 2 minutes.
  Patients see no difference (their ticket says "Assigned automatically").

## 4. n8n setup (folder `n8n/`)
1. Start n8n (e.g. Docker: `docker run -d --name n8n --restart unless-stopped -p 5678:5678 -v n8n_data:/home/node/.n8n docker.n8n.io/n8nio/n8n`).
2. In n8n: create a workflow, then **... -> Import from File** and pick `1_MediKiosk_Backend_...json` (this is the one the website uses).
   `2_Receptionist_Booking_...json` is the separate n8n-only version with a form - import it only if you want that demo, and publish only ONE copy of it.
3. Re-link credentials: OpenAI node (base URL of your local model, e.g. `http://host.docker.internal:11434/v1`), Gmail OAuth2 (or swap for Send Email/SMTP), Google Sheets.
4. Google Sheets: the sheet must be a real Google Sheet (not an uploaded .xlsx - use File -> Save as Google Sheets) with a tab `Bookings`
   and headers from `docs/MediKiosk_sheet_template.xlsx`.
5. Click **Publish**. Test (PowerShell):
```
Invoke-RestMethod -Method Post -Uri http://localhost:5678/webhook/medikiosk-triage -ContentType "application/json" -Body '{"name":"Ravi","age":55,"gender":"M","symptoms":"severe chest pain"}'
```
   It should answer with the ward. Look under **Executions** to see the runs.

## 5. Databases
- Local file (default): nothing to set up.
- **Neon (free PostgreSQL):** create a project, turn OFF "Connection pooling" in the Connect dialog, copy the string into DATABASE_URL.
  The website lets the free database sleep. Free Neon has only a short restore window: use Staff desk -> Download Excel regularly.
- MySQL (e.g. Aiven): `mysql://user:password@host:port/db?ssl-mode=REQUIRED`.
Data in one database is not copied to another automatically.

## 6. Deploying (Render example)
Build `pip install -r requirements.txt`, start `gunicorn app:app -c gunicorn.conf.py`. Set the settings above as environment variables.
Free hosts sleep, lose uploaded files on restart and block Gmail SMTP: for a real hospital use a paid always-on plan with a persistent disk (DATA_DIR) and Brevo or another web e-mail API.
Render cannot reach `localhost` - use USE_N8N=0 there, or put n8n on a public address.

## 7. Staff desk
Doctors (change hours / availability, updates the Excel file) - Receipt (print / PDF, shows "OP will be active up to 6 days" and the last active date) -
Patients (adherence score per patient) - Download Excel / Export CSV - Send test e-mail.

## 8. If something does not work
| Problem | Check |
|---|---|
| n8n lines missing on Staff desk | n8n is failing or off; the website is using its own logic (this is intended) |
| No appointment e-mails | set BREVO_API_KEY + EMAIL_FROM or the SMTP_* settings; use "Send test e-mail" |
| Google Sheet log FAILED | share the sheet with the service-account e-mail as Editor; tab named Bookings; real Google Sheet |
| AI always falls back | raise N8N_TRIAGE_TIMEOUT; keep the model loaded (`ollama run qwen2.5:7b`) |
| Wrong times on a server | APP_TIMEZONE (default Asia/Kolkata) |
| Database error on start | check DATABASE_URL (special characters in the password must be URL-encoded) |

## 9. Known limits
- A shift that runs past midnight (e.g. 12 PM - 1 AM) is bookable only until midnight.
- The report checker compares values with typical ranges; it is not a diagnosis. Photos / scans cannot be read.
- A small local AI model can occasionally choose the wrong ward; staff can see the problem text on every booking.
