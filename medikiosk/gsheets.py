"""
gsheets.py - the website writes each booking to your Google Sheet by itself (used when n8n is off or down).

One-time setup
  1. Google Cloud (your project) -> APIs & Services -> Library -> enable "Google Sheets API".
  2. IAM & Admin -> Service accounts -> Create service account -> open it -> Keys -> Add key -> JSON (a file downloads).
  3. Open your Google Sheet (a real Google Sheet, not an uploaded .xlsx) -> Share -> add the service account's e-mail
     (looks like name@project.iam.gserviceaccount.com) as Editor.
  4. Settings (.env):  GOOGLE_SERVICE_ACCOUNT_JSON = path of the downloaded .json file (or the JSON text itself)
                       GOOGLE_SHEET_ID = the long id in the sheet's address  .../spreadsheets/d/<THIS PART>/edit
                       GOOGLE_SHEET_TAB = Bookings   (default)
The tab needs this header row:  Appointment ID | Date | Time | Token | Patient Name | Phone | Symptoms | Ward Name | Doctor Name | Room
No extra packages: the sign-in is done with the `cryptography` package the project already uses.
"""
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import services as svc

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
_token = {"value": None, "expires": 0.0}


def configured():
    return bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip() and os.environ.get("GOOGLE_SHEET_ID", "").strip())


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _load_key():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw.startswith("{"):
        return json.loads(raw)
    with open(raw, encoding="utf-8") as fh:
        return json.load(fh)


def _access_token():
    if _token["value"] and time.time() < _token["expires"] - 60:
        return _token["value"]
    info = _load_key()
    token_uri = os.environ.get("GOOGLE_TOKEN_URL") or info.get("token_uri") or "https://oauth2.googleapis.com/token"
    now = int(time.time())
    signing_input = (_b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + "." +
                     _b64(json.dumps({"iss": info["client_email"], "scope": SCOPE, "aud": token_uri,
                                      "iat": now, "exp": now + 3600}).encode()))
    key = serialization.load_pem_private_key(info["private_key"].encode(), password=None)
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                   "assertion": signing_input + "." + _b64(signature)}).encode()
    req = urllib.request.Request(token_uri, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    _token.update(value=data["access_token"], expires=time.time() + int(data.get("expires_in", 3600)))
    return _token["value"]


def _friendly(code, detail):
    if code == 403:
        return "no permission - share the Google Sheet with the service account e-mail as Editor"
    if code == 404:
        return "sheet not found - check GOOGLE_SHEET_ID"
    if code == 400 and "range" in detail.lower():
        return "tab not found - create a tab named Bookings (or set GOOGLE_SHEET_TAB)"
    if code == 400 and "not supported" in detail.lower():
        return "this file is an uploaded Excel file - use File > Save as Google Sheets"
    return f"Google answered {code}"


def append_row(d):
    tab = os.environ.get("GOOGLE_SHEET_TAB", "Bookings").strip() or "Bookings"
    base = os.environ.get("GOOGLE_SHEETS_API", "https://sheets.googleapis.com/v4/spreadsheets").rstrip("/")
    sheet_id = os.environ["GOOGLE_SHEET_ID"].strip()
    url = (f"{base}/{sheet_id}/values/{urllib.parse.quote(tab + '!A:J')}:append"
           "?valueInputOption=RAW&insertDataOption=INSERT_ROWS")
    row = [str(d.get(k, "")) for k in ("appointment_id", "date", "time", "token", "patient_name", "patient_phone",
                                       "symptoms", "ward", "doctor_name", "room")]
    req = urllib.request.Request(url, data=json.dumps({"values": [row]}).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + _access_token(),
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "ignore")
        raise RuntimeError(_friendly(exc.code, detail)) from None


def log_async(d):
    """Fire-and-forget, like the e-mails: a booking never waits for Google."""
    if not configured():
        return

    def run():
        try:
            append_row(d)
            svc.STATUS["sheet"] = f"OK - last booking added to the Google Sheet [{svc.now_local().strftime('%d %b %H:%M')}]"
        except Exception as exc:
            svc.STATUS["sheet"] = f"FAILED: {exc}"
            print(f"[google-sheet] {exc}")

    threading.Thread(target=run, daemon=True).start()
