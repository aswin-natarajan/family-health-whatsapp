import os
import json
import logging
import requests
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import pytz
import base64
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
WHATSAPP_TOKEN      = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID   = os.environ.get("WHATSAPP_PHONE_ID")
VERIFY_TOKEN        = os.environ.get("VERIFY_TOKEN", "family_health_verify_123")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY")
SPREADSHEET_ID      = os.environ.get("SPREADSHEET_ID")
GOOGLE_CREDS_JSON   = os.environ.get("GOOGLE_CREDS_JSON")

IST = pytz.timezone("Asia/Kolkata")

# ── Family members ─────────────────────────────────────────────────────────────
# To add more members later, add entries here in the format:
# "91XXXXXXXXXX": {"name": "Name", "medications": [...]}
FAMILY_MEMBERS = {
    # Replace with BN's actual WhatsApp number (with country code, no + or spaces)
    os.environ.get("BN_PHONE", "910000000000"): {
        "name": "BN",
        "medications": [
            {"time": "07:00", "name": "Morning medications"},
            {"time": "09:00", "name": "9 AM medications"},
            {"time": "10:00", "name": "10 AM medications"},
            {"time": "12:00", "name": "Noon medications"},
            {"time": "17:00", "name": "5 PM medications"},
            {"time": "20:00", "name": "8 PM medications"},
            {"time": "21:00", "name": "9 PM medications"},
            {"time": "22:00", "name": "10 PM medications"},
        ]
    }
}

# Admin number — you (Aswin) get summaries and alerts
ADMIN_PHONE = os.environ.get("ADMIN_PHONE", "600000000000")

# ── Google Sheets ──────────────────────────────────────────────────────────────
def get_sheets_client():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

def ensure_sheet_headers(worksheet, headers):
    existing = worksheet.row_values(1)
    if not existing:
        worksheet.append_row(headers)

def log_to_sheet(sheet_name, row_data):
    try:
        gc = get_sheets_client()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=20)
        
        headers_map = {
            "Vitals":      ["Timestamp", "Person", "Vital Type", "Value", "Unit", "Notes", "Source"],
            "Medications": ["Timestamp", "Person", "Scheduled Time", "Status", "Notes"],
            "Lab Results": ["Timestamp", "Person", "Test Name", "Value", "Unit", "Reference Range", "Notes"],
            "Messages":    ["Timestamp", "Person", "Direction", "Message"],
        }
        if sheet_name in headers_map:
            ensure_sheet_headers(ws, headers_map[sheet_name])
        
        ws.append_row(row_data)
        logger.info(f"Logged to sheet '{sheet_name}': {row_data}")
    except Exception as e:
        logger.error(f"Sheet logging error: {e}")

# ── WhatsApp API ───────────────────────────────────────────────────────────────
def send_whatsapp_message(to, message):
    url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code != 200:
        logger.error(f"WhatsApp send error: {resp.text}")
    return resp

def get_media_url(media_id):
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    resp = requests.get(url, headers=headers)
    return resp.json().get("url")

def download_media(media_url):
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    resp = requests.get(media_url, headers=headers)
    return resp.content

# ── Claude AI ──────────────────────────────────────────────────────────────────
def parse_message_with_claude(person_name, message_text=None, image_data=None, image_mime=None):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    
    system_prompt = """You are a health data extraction assistant for a family health monitoring system.
Your job is to extract structured health data from messages sent by elderly family members via WhatsApp.

Extract the following if present:
- Vitals: blood pressure (systolic/diastolic), blood sugar (fasting/post-meal), weight, heart rate, SpO2
- Lab results: creatinine, eGFR, urea, potassium, cholesterol (total/LDL/HDL), haemoglobin
- Medication confirmation: did they take their medication?
- General health notes

Respond ONLY with a JSON object in this exact format:
{
  "type": "vitals" | "lab_results" | "medication_confirmation" | "general_note" | "unknown",
  "medication_taken": true | false | null,
  "vitals": [
    {"name": "blood_pressure", "systolic": 120, "diastolic": 80, "unit": "mmHg"},
    {"name": "blood_sugar", "value": 95, "unit": "mg/dL", "context": "fasting"},
    {"name": "weight", "value": 70, "unit": "kg"},
    {"name": "heart_rate", "value": 72, "unit": "bpm"},
    {"name": "spo2", "value": 98, "unit": "%"}
  ],
  "lab_results": [
    {"name": "creatinine", "value": 1.1, "unit": "mg/dL", "reference": "0.7-1.3"},
    {"name": "egfr", "value": 75, "unit": "mL/min/1.73m²"},
    {"name": "urea", "value": 25, "unit": "mg/dL"},
    {"name": "potassium", "value": 4.2, "unit": "mEq/L"},
    {"name": "cholesterol_total", "value": 180, "unit": "mg/dL"},
    {"name": "cholesterol_ldl", "value": 100, "unit": "mg/dL"},
    {"name": "cholesterol_hdl", "value": 50, "unit": "mg/dL"},
    {"name": "haemoglobin", "value": 13.5, "unit": "g/dL"}
  ],
  "notes": "any additional context",
  "reply": "a warm, brief acknowledgment in simple English for an elderly person"
}

Only include fields that are actually present. Keep the reply warm and simple."""

    content = []
    if image_data:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image_mime or "image/jpeg",
                "data": image_data
            }
        })
    if message_text:
        content.append({"type": "text", "text": f"Message from {person_name}: {message_text}"})
    elif image_data:
        content.append({"type": "text", "text": f"This is a health report image sent by {person_name}. Please extract all health values visible."})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        system=system_prompt,
        messages=[{"role": "user", "content": content}]
    )
    
    raw = response.content[0].text
    # Strip markdown code fences if present
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)

# ── Process incoming message ───────────────────────────────────────────────────
def process_incoming_message(from_number, message_text=None, image_media_id=None, image_mime=None):
    person = FAMILY_MEMBERS.get(from_number)
    person_name = person["name"] if person else from_number
    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    # Log raw message
    log_to_sheet("Messages", [now_ist, person_name, "Incoming", message_text or "[image]"])

    # Download image if present
    image_b64 = None
    if image_media_id:
        try:
            media_url = get_media_url(image_media_id)
            image_bytes = download_media(media_url)
            image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        except Exception as e:
            logger.error(f"Image download error: {e}")

    # Parse with Claude
    try:
        parsed = parse_message_with_claude(person_name, message_text, image_b64, image_mime)
    except Exception as e:
        logger.error(f"Claude parse error: {e}")
        send_whatsapp_message(from_number, "Got your message! I had a little trouble reading it. Please try again or type your reading manually.")
        return

    # Log vitals
    for vital in parsed.get("vitals", []):
        if vital["name"] == "blood_pressure":
            log_to_sheet("Vitals", [now_ist, person_name, "Blood Pressure",
                f"{vital.get('systolic')}/{vital.get('diastolic')}", "mmHg", parsed.get("notes",""), "WhatsApp"])
        else:
            log_to_sheet("Vitals", [now_ist, person_name, vital["name"].replace("_"," ").title(),
                vital.get("value"), vital.get("unit",""), parsed.get("notes",""), "WhatsApp"])

    # Log lab results
    for lab in parsed.get("lab_results", []):
        log_to_sheet("Lab Results", [now_ist, person_name, lab["name"].replace("_"," ").title(),
            lab.get("value"), lab.get("unit",""), lab.get("reference",""), parsed.get("notes","")])

    # Log medication confirmation
    if parsed.get("medication_taken") is True:
        log_to_sheet("Medications", [now_ist, person_name, "N/A", "Confirmed taken", parsed.get("notes","")])
        # Notify admin
        send_whatsapp_message(ADMIN_PHONE, f"✅ {person_name} confirmed medication taken at {now_ist}")

    # Reply to user
    reply = parsed.get("reply", "Got it, thank you! 🙏")
    send_whatsapp_message(from_number, reply)
    log_to_sheet("Messages", [now_ist, person_name, "Outgoing", reply])

# ── Medication reminders ───────────────────────────────────────────────────────
def send_reminder(phone, name, med_name):
    message = f"🔔 Reminder for {name}: Time to take your *{med_name}*. Please reply *done* when you've taken them. 💊"
    send_whatsapp_message(phone, message)
    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    log_to_sheet("Medications", [now_ist, name, med_name, "Reminder sent", ""])
    logger.info(f"Reminder sent to {name} for {med_name}")

def setup_reminders(scheduler):
    for phone, member in FAMILY_MEMBERS.items():
        for med in member["medications"]:
            hour, minute = map(int, med["time"].split(":"))
            scheduler.add_job(
                send_reminder,
                CronTrigger(hour=hour, minute=minute, timezone=IST),
                args=[phone, member["name"], med["name"]],
                id=f"reminder_{member['name']}_{med['time']}",
                replace_existing=True
            )
            logger.info(f"Scheduled reminder for {member['name']} at {med['time']} IST")

# ── Webhook ────────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def receive_message():
    data = request.get_json()
    try:
        entry   = data["entry"][0]
        changes = entry["changes"][0]["value"]
        
        if "messages" not in changes:
            return jsonify({"status": "ok"}), 200

        msg        = changes["messages"][0]
        from_phone = msg["from"]
        msg_type   = msg["type"]

        if msg_type == "text":
            text = msg["text"]["body"]
            process_incoming_message(from_phone, message_text=text)

        elif msg_type == "image":
            media_id   = msg["image"]["id"]
            mime_type  = msg["image"].get("mime_type", "image/jpeg")
            caption    = msg["image"].get("caption", "")
            process_incoming_message(from_phone, message_text=caption if caption else None,
                                     image_media_id=media_id, image_mime=mime_type)

        elif msg_type == "document":
            media_id  = msg["document"]["id"]
            mime_type = msg["document"].get("mime_type", "application/pdf")
            caption   = msg["document"].get("caption", "")
            process_incoming_message(from_phone, message_text=caption if caption else None,
                                     image_media_id=media_id, image_mime=mime_type)

    except Exception as e:
        logger.error(f"Webhook processing error: {e}")

    return jsonify({"status": "ok"}), 200

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "running", "time_ist": datetime.now(IST).isoformat()}), 200

# ── Start ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    setup_reminders(scheduler)
    scheduler.start()
    logger.info("Scheduler started with all reminders")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
