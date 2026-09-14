import json
import base64
from datetime import datetime, time, timedelta, timezone
from flask import Flask, jsonify, request
import firebase_admin
from firebase_admin import credentials, firestore
import os

LOCAL_TZ = timezone(timedelta(hours=5, minutes=30))
DEFAULT_SHIFT_START = time(9, 0, 0)
DEFAULT_GRACE_MINS = 30
HALF_DAY_THRESHOLD_HOURS = 4.5
recent_scans = {}
DEBOUNCE_SECONDS = 60

cred_b64 = os.environ.get("FIREBASE_CREDS_B64")
if cred_b64:
    cred_dict = json.loads(base64.b64decode(cred_b64))
    cred = credentials.Certificate(cred_dict)
else:
    cred = credentials.Certificate("serviceAccountKey.json")

firebase_admin.initialize_app(cred)
db = firestore.client()

app = Flask(__name__)

def process_biometric_punch(user_id: str, punch_time: datetime, employee_name: str = "Employee"):
    date_str = punch_time.strftime("%Y-%m-%d")
    month_str = punch_time.strftime("%Y-%m")
    time_str = punch_time.strftime("%H:%M:%S")

    print(f"\n📌 [PUNCH RECEIVED] User: {user_id} | Date: {date_str} | Time: {time_str}")

    try:
        user_doc = db.collection("users").document(user_id).get()
        shift_time = DEFAULT_SHIFT_START
        monthly_grace_allowed = DEFAULT_GRACE_MINS

        if user_doc.exists:
            u_data = user_doc.to_dict() or {}
            employee_name = u_data.get("name") or employee_name
            shift = u_data.get("shift", {})
            if shift.get("startTime"):
                sh, sm = map(int, shift["startTime"].split(":")[:2])
                shift_time = time(sh, sm, 0)
            if shift.get("monthlyGraceAllowance") is not None:
                monthly_grace_allowed = int(shift["monthlyGraceAllowance"])

        daily_ref = db.collection("daily_attendance").document(f"{date_str}_{user_id}")
        daily_doc = daily_ref.get()

        monthly_ref = db.collection("monthly_summaries").document(f"{month_str}_{user_id}")
        monthly_doc = monthly_ref.get()

        if monthly_doc.exists:
            m_data = monthly_doc.to_dict() or {}
            grace_remaining = m_data.get("graceRemaining", monthly_grace_allowed)
            grace_used = m_data.get("graceUsed", 0)
            total_late_mins = m_data.get("totalLateMinutes", 0)
            present_days = m_data.get("presentDays", 0)
            late_days = m_data.get("lateDays", 0)
        else:
            grace_remaining = monthly_grace_allowed
            grace_used = 0
            total_late_mins = 0
            present_days = 0
            late_days = 0

        now_utc = datetime.now(timezone.utc).isoformat()

        if not daily_doc.exists:
            shift_start_dt = datetime.combine(punch_time.date(), shift_time).replace(tzinfo=LOCAL_TZ)
            delay_seconds = (punch_time - shift_start_dt).total_seconds()
            minutes_delayed = max(0, int(delay_seconds // 60))

            grace_deducted = 0
            late_minutes = 0
            status = "On Time"

            if minutes_delayed > 0:
                if grace_remaining >= minutes_delayed:
                    grace_deducted = minutes_delayed
                    grace_remaining -= minutes_delayed
                    grace_used += minutes_delayed
                    status = "Grace Used"
                elif grace_remaining > 0:
                    grace_deducted = grace_remaining
                    late_minutes = minutes_delayed - grace_remaining
                    grace_used += grace_remaining
                    grace_remaining = 0
                    total_late_mins += late_minutes
                    late_days += 1
                    status = "Late"
                else:
                    late_minutes = minutes_delayed
                    total_late_mins += late_minutes
                    late_days += 1
                    status = "Late"

            present_days += 1

            daily_ref.set({
                "userId": user_id,
                "name": employee_name,
                "date": date_str,
                "month": month_str,
                "checkIn": time_str,
                "checkOut": None,
                "scheduledCheckIn": shift_time.strftime("%H:%M:%S"),
                "minutesDelayed": minutes_delayed,
                "graceDeducted": grace_deducted,
                "status": status,
                "totalWorkingHours": 0.0,
                "createdAt": now_utc,
                "updatedAt": now_utc,
            })

            monthly_ref.set({
                "month": month_str,
                "userId": user_id,
                "name": employee_name,
                "graceTotalAllowed": monthly_grace_allowed,
                "graceUsed": grace_used,
                "graceRemaining": grace_remaining,
                "totalLateMinutes": total_late_mins,
                "presentDays": present_days,
                "lateDays": late_days,
                "updatedAt": now_utc,
            }, merge=True)
            print(f"✅ Check-in recorded: {time_str} ({status})")
        else:
            d_data = daily_doc.to_dict() or {}
            check_in_str = d_data.get("checkIn") or time_str
            current_status = d_data.get("status", "On Time")

            check_in_dt = datetime.strptime(f"{date_str} {check_in_str}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ)
            worked_seconds = max(0, (punch_time - check_in_dt).total_seconds())
            working_hours = round(worked_seconds / 3600, 2)

            if working_hours < HALF_DAY_THRESHOLD_HOURS and current_status not in ["Late", "Half Day"]:
                current_status = "Half Day"

            daily_ref.update({
                "checkOut": time_str,
                "totalWorkingHours": working_hours,
                "status": current_status,
                "updatedAt": now_utc,
            })
            print(f"✅ Check-out updated: {time_str} ({working_hours} hrs)")
    except Exception as e:
        print(f"❌ Error processing attendance: {e}")

@app.before_request
def log_incoming_request():
    print(f"\n🌐 [INCOMING] {request.method} {request.path} from {request.remote_addr}")
    if request.data:
        print(f"   Payload: {request.get_data(as_text=True)[:300]}")

@app.route("/", methods=["GET", "POST"])
@app.route("/api/attendance", methods=["GET", "POST"])
@app.route("/api/attendance/", methods=["GET", "POST"])
def dahua_webhook():
    if request.method == "GET":
        return jsonify({"status": "active", "service": "Dahua Biometric Receiver"}), 200

    payload = request.get_json(silent=True)
    if not payload and request.data:
        try:
            payload = json.loads(request.data.decode("utf-8"))
        except Exception:
            pass

    if not payload:
        print("🔍 Received empty probe / connection test from Dahua")
        return jsonify({"code": 200, "message": "Test successful"}), 200

    code = payload.get("Code")
    data = payload.get("Data", {})

    if code == "AccessControl" and data.get("Status") == 1:
        user_id = str(data.get("UserID", "")).strip()
        employee_name = data.get("CardName") or "Employee"

        if not user_id:
            return jsonify({"code": 400, "message": "Missing UserID"}), 400

        utc_ts = data.get("UTC") or data.get("CreateTime")
        punch_time = datetime.fromtimestamp(utc_ts, tz=LOCAL_TZ) if utc_ts else datetime.now(tz=LOCAL_TZ)

        now_epoch = int(punch_time.timestamp())
        if (now_epoch - recent_scans.get(user_id, 0)) < DEBOUNCE_SECONDS:
            return jsonify({"code": 200, "message": "Debounced"}), 200

        recent_scans[user_id] = now_epoch
        process_biometric_punch(user_id, punch_time, employee_name)

    return jsonify({"code": 200, "message": "Success"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)