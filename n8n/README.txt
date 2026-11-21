n8n workflows for MediKiosk
===========================
1_MediKiosk_Backend_AI_Triage_and_Booking_Notifications.json
   USED BY THE WEBSITE. Two flows:
   - Triage Request (webhook /medikiosk-triage) -> Message a model (AI) -> Send Ward to Website
   - Appointment Booked (webhook /medikiosk-booked) -> Email Doctor -> Email Patient -> Email Receptionist -> Log Booking in Sheet
   Every e-mail / sheet node is set to "continue on error", so one failure never stops the others.
   Change the receptionist address inside the "Email Receptionist" node.

2_Receptionist_Booking_Queue_Room_Time_n8n_only.json
   Separate, n8n-only version with a web form (no website needed): AI triage -> doctor + token + queue (Google Sheet) ->
   e-mails to doctor, patient and receptionist -> confirmation page; alert if no doctor; a 15-minute timer frees doctors.
   Its form address is /form/neelima-hospital-booking: publish only ONE copy of this workflow.

How to import: n8n -> create a workflow -> "..." menu -> Import from File. Re-link the credentials (OpenAI, Gmail, Google Sheets),
open the Google Sheets nodes and pick your spreadsheet (must be a real Google Sheet with the tab names the node expects), then Publish.
