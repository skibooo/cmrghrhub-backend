from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client
import os
from dotenv import load_dotenv
from fastapi import UploadFile, File
import pandas as pd
import io
from datetime import datetime, date, time
from collections import defaultdict
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from apscheduler.schedulers.background import BackgroundScheduler
from google import genai

CUTOFF_TIME = time(8, 30)  # 8:30 AM

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this to your Vercel URL once deployed
    allow_methods=["*"],
    allow_headers=["*"],
)

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")
supabase = create_client(url, key)

@app.get("/")
def read_root():
    return {"status": "backend is running"}

@app.get("/test-db")
def test_db():
    response = supabase.table("departments").select("*").execute()
    return {"departments": response.data}

from pydantic import BaseModel
import uuid

class Department(BaseModel):
    name: str

class Staff(BaseModel):
    name: str
    department_id: str

# --- Departments ---
@app.get("/departments")
def get_departments():
    response = supabase.table("departments").select("*").execute()
    return response.data

@app.post("/departments")
def create_department(dept: Department):
    response = supabase.table("departments").insert({"name": dept.name}).execute()
    return response.data

# --- Staff ---
@app.get("/staff")
def get_staff():
    response = supabase.table("staff").select("*").execute()
    return response.data

@app.post("/staff")
def create_staff(staff: Staff):
    staff_code = str(uuid.uuid4())[:8].upper()
    response = supabase.table("staff").insert({
        "staff_code": staff_code,
        "name": staff.name,
        "department_id": staff.department_id,
        "status": "pending"
    }).execute()
    return response.data

@app.post("/staff/bulk-upload")
async def bulk_upload_staff(file: UploadFile = File(...)):
    contents = await file.read()

    if file.filename.endswith(".csv"):
        df = pd.read_csv(io.BytesIO(contents))
    else:
        df = pd.read_excel(io.BytesIO(contents))

    created = []
    errors = []

    for _, row in df.iterrows():
        try:
            name = str(row["name"]).strip()
            dept_name = str(row["department"]).strip()

            # find or create department
            dept_resp = supabase.table("departments").select("*").eq("name", dept_name).execute()
            if dept_resp.data:
                dept_id = dept_resp.data[0]["id"]
            else:
                new_dept = supabase.table("departments").insert({"name": dept_name}).execute()
                dept_id = new_dept.data[0]["id"]

            staff_code = str(uuid.uuid4())[:8].upper()
            supabase.table("staff").insert({
                "staff_code": staff_code,
                "name": name,
                "department_id": dept_id,
                "status": "pending"
            }).execute()
            created.append(name)
        except Exception as e:
            errors.append({"row": name if 'name' in locals() else "unknown", "error": str(e)})

    return {"created": created, "errors": errors}


class ClockIn(BaseModel):
    staff_id: str
    late_reason: str | None = None

class ClockOut(BaseModel):
    staff_id: str

@app.post("/attendance/clock-in")
def clock_in(payload: ClockIn):
    if payload.office_code != OFFICE_CODE:
     return {"error": "invalid_office_code", "message": "Incorrect office code. Make sure you're at the office."}
    now = datetime.now()
    today = now.date().isoformat()
    is_late = now.time() > CUTOFF_TIME

    if is_late and not payload.late_reason:
        return {"error": "late_reason_required", "message": "You're clocking in after 8:30 AM. Please provide a reason."}

    existing = supabase.table("attendance_logs").select("*").eq("staff_id", payload.staff_id).eq("date", today).execute()
    if existing.data:
        return {"error": "already_clocked_in", "message": "You've already clocked in today."}

    response = supabase.table("attendance_logs").insert({
        "staff_id": payload.staff_id,
        "date": today,
        "clock_in": now.isoformat(),
        "is_late": is_late,
        "late_reason": payload.late_reason,
        "approval_status": "pending" if is_late else None
    }).execute()
    return response.data

@app.post("/attendance/clock-out")
def clock_out(payload: ClockOut):
    now = datetime.now()
    today = now.date().isoformat()

    existing = supabase.table("attendance_logs").select("*").eq("staff_id", payload.staff_id).eq("date", today).execute()
    if not existing.data:
        return {"error": "not_clocked_in", "message": "You haven't clocked in today."}

    log_id = existing.data[0]["id"]
    response = supabase.table("attendance_logs").update({
        "clock_out": now.isoformat()
    }).eq("id", log_id).execute()
    return response.data

@app.get("/attendance/today")
def get_today_attendance():
    today = date.today().isoformat()
    response = supabase.table("attendance_logs").select("*, staff(name, department_id)").eq("date", today).execute()
    return response.data

class ApprovalUpdate(BaseModel):
    approval_status: str  # "approved" or "denied"

@app.get("/attendance/pending-approvals")
def get_pending_approvals():
    response = supabase.table("attendance_logs").select("*, staff(name, department_id)").eq("approval_status", "pending").execute()
    return response.data

@app.patch("/attendance/{log_id}/approval")
def update_approval(log_id: str, payload: ApprovalUpdate):
    if payload.approval_status not in ["approved", "denied"]:
        return {"error": "invalid_status", "message": "Status must be 'approved' or 'denied'."}

    response = supabase.table("attendance_logs").update({
        "approval_status": payload.approval_status
    }).eq("id", log_id).execute()
    return response.data


@app.get("/analytics/summary")
def get_analytics_summary(period: str = "daily", start_date: str = None, end_date: str = None):
    """
    period: 'daily', 'weekly', 'monthly', 'yearly'
    If start_date/end_date not given, defaults to today for daily,
    current week/month/year otherwise.
    """
    today = date.today()

    if not start_date or not end_date:
        if period == "daily":
            start_date = end_date = today.isoformat()
        elif period == "weekly":
            start_date = (today - pd.Timedelta(days=today.weekday())).isoformat()
            end_date = today.isoformat()
        elif period == "monthly":
            start_date = today.replace(day=1).isoformat()
            end_date = today.isoformat()
        elif period == "yearly":
            start_date = today.replace(month=1, day=1).isoformat()
            end_date = today.isoformat()

    logs = supabase.table("attendance_logs").select("*, staff(name, department_id, departments(name))").gte("date", start_date).lte("date", end_date).execute().data

    total_logs = len(logs)
    on_time = sum(1 for l in logs if not l["is_late"])
    late = sum(1 for l in logs if l["is_late"])

    # total hours per staff
    hours_per_staff = defaultdict(float)
    dept_breakdown = defaultdict(lambda: {"present": 0, "late": 0})

    for l in logs:
        if l["clock_in"] and l["clock_out"]:
            ci = datetime.fromisoformat(l["clock_in"])
            co = datetime.fromisoformat(l["clock_out"])
            hours = (co - ci).total_seconds() / 3600
            staff_name = l["staff"]["name"] if l.get("staff") else "Unknown"
            hours_per_staff[staff_name] += hours

        dept_name = "Unknown"
        if l.get("staff") and l["staff"].get("departments"):
            dept_name = l["staff"]["departments"]["name"]
        dept_breakdown[dept_name]["present"] += 1
        if l["is_late"]:
            dept_breakdown[dept_name]["late"] += 1

    return {
        "period": period,
        "start_date": start_date,
        "end_date": end_date,
        "total_logs": total_logs,
        "on_time": on_time,
        "late": late,
        "hours_per_staff": hours_per_staff,
        "department_breakdown": dept_breakdown
    }

@app.get("/analytics/export")
def export_logs():
    logs = supabase.table("attendance_logs").select("*, staff(name, staff_code, departments(name))").execute().data
    return logs


def send_daily_report():
    today = date.today().isoformat()
    logs = supabase.table("attendance_logs").select("*, staff(name, departments(name))").eq("date", today).execute().data

    total = len(logs)
    late = sum(1 for l in logs if l["is_late"])
    on_time = total - late

    body = f"Attendance Summary for {today}\n\nTotal Clock-ins: {total}\nOn Time: {on_time}\nLate: {late}\n\n"
    for l in logs:
        name = l["staff"]["name"] if l.get("staff") else "Unknown"
        body += f"- {name}: {'LATE' if l['is_late'] else 'On Time'}\n"

    msg = MIMEMultipart()
    msg["From"] = os.environ.get("SMTP_USER")
    msg["To"] = os.environ.get("REPORT_RECIPIENT")
    msg["Subject"] = f"CMRGHRHUB Attendance Summary - {today}"
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST"), int(os.environ.get("SMTP_PORT"))) as server:
        server.login(os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS"))
        server.send_message(msg)

@app.post("/reports/send-now")
def trigger_report_now():
    send_daily_report()
    return {"status": "sent"}

scheduler = BackgroundScheduler()
scheduler.add_job(send_daily_report, "cron", hour=9, minute=0)
scheduler.start()

gemini_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

class AIRequest(BaseModel):
    prompt: str

@app.post("/ai/ask")
def ask_ai(payload: AIRequest):
    response = gemini_client.models.generate_content(
        model="gemini-3-flash-preview",
        contents=payload.prompt
    )
    return {"response": response.text}

class ClaimRequest(BaseModel):
    staff_id: str
    pin: str

class SignupRequest(BaseModel):
    name: str
    department_id: str
    pin: str

@app.get("/staff/search")
def search_staff(name: str):
    response = supabase.table("staff").select("*").ilike("name", f"%{name}%").execute()
    return response.data

@app.post("/staff/claim")
def claim_staff(payload: ClaimRequest):
    existing = supabase.table("staff").select("*").eq("id", payload.staff_id).execute()
    if not existing.data:
        return {"error": "not_found", "message": "Staff record not found."}
    if existing.data[0]["status"] == "active":
        return {"error": "already_claimed", "message": "This account has already been set up."}

    response = supabase.table("staff").update({
        "pin": payload.pin,
        "status": "active"
    }).eq("id", payload.staff_id).execute()
    return response.data

@app.post("/staff/signup")
def signup_staff(payload: SignupRequest):
    staff_code = str(uuid.uuid4())[:8].upper()
    response = supabase.table("staff").insert({
        "staff_code": staff_code,
        "name": payload.name,
        "department_id": payload.department_id,
        "pin": payload.pin,
        "status": "active"
    }).execute()
    return response.data

OFFICE_CODE = "CMRG-2607"  # change this daily, or we can automate it later

class ClockIn(BaseModel):
    staff_id: str
    late_reason: str | None = None
    office_code: str