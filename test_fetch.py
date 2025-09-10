# outgate_app.py
import io
import os
import re
import csv
import hashlib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# -----------------------------
# Configuration
# -----------------------------
st.set_page_config(page_title="Student Out-Gate Manager", page_icon="🚪", layout="centered")

INDIA_TZ = ZoneInfo("Asia/Kolkata")
TODAY = date.today()

# Hard cap constraints
MAX_DURATION_DAYS = 14              # Max length of out-gate
MAX_FUTURE_DAYS = 60                # Latest end date allowed from today
MAX_REASON_LEN = 400                # Reason length cap
MIN_REASON_LEN = 10                 # Require at least some explanation
MAX_UPLOAD_MB = 5
ALLOWED_FILE_TYPES = ["pdf", "png", "jpg", "jpeg"]

# Data file location (adjust if needed)
CANDIDATE_PATHS = [
    "students_master_data.csv",
]

# Logging (kept minimal & local; ensure server-side access control in production)
ENABLE_LOGGING = True
LOG_DIR = "secure_logs"
LOG_PATH = os.path.join(LOG_DIR, "outgate_requests_log.csv")

# -----------------------------
# Helpers
# -----------------------------
def find_data_path():
    for p in CANDIDATE_PATHS:
        if os.path.exists(p):
            return p
    return None

def normalize_col(col: str) -> str:
    # collapse inner whitespace, strip, lowercase
    return " ".join(col.strip().split()).lower()

def load_db():
    path = find_data_path()
    if not path:
        raise FileNotFoundError("Student master CSV not found. Looked for: " + ", ".join(CANDIDATE_PATHS))
    # Read strictly as strings to preserve formatting like leading zeros
    df = pd.read_csv(path, dtype=str).fillna("")
    # Build a mapping of normalized->actual names
    norm_map = {normalize_col(c): c for c in df.columns}
    return df, norm_map, path

def require_columns(norm_map: dict, required_normals: list[str]):
    missing = [r for r in required_normals if r not in norm_map]
    if missing:
        raise KeyError("CSV is missing required columns: " + ", ".join(missing))

def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 4:
        return "••••"
    return f"{'•' * (len(digits)-4)}{digits[-4:]}"

def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return "—"
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        masked_local = local[0] + "•" * max(0, len(local)-1)
    else:
        masked_local = local[0] + "•" * (len(local)-2) + local[-1]
    # Only show domain TLD, mask second-level domain partly
    parts = domain.split(".")
    if len(parts) >= 2:
        sld = parts[-2]
        masked_sld = sld[0] + "•" * max(0, len(sld)-2) + sld[-1] if len(sld) >= 2 else "•"
        masked_domain = ".".join([masked_sld, parts[-1]])
    else:
        masked_domain = "•" * len(domain)
    return f"{masked_local}@{masked_domain}"

def validate_email(email: str) -> bool:
    if not email:
        return False
    pat = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
    return bool(pat.match(email.strip()))

def find_student_by_email(df: pd.DataFrame, norm_map: dict, email: str) -> pd.DataFrame:
    key = norm_map["student email"]
    # Case-insensitive match after trimming
    em = email.strip().casefold()
    series = df[key].fillna("").astype(str).map(lambda x: x.strip().casefold())
    hits = df[series == em]
    return hits

def sha256_of_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def append_log(row: dict):
    if not ENABLE_LOGGING:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    file_exists = os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

# -----------------------------
# UI
# -----------------------------
st.title("Student Out-Gate Management")
st.caption("Securely request and track student out-gate passes.")

with st.expander("ℹ️ How this works", expanded=False):
    st.markdown(
        "- Enter the **student email** and request details below.\n"
        "- The app fetches student & parent information **directly from the master CSV**.\n"
        "- Sensitive fields are **masked** in the preview.\n"
        "- Uploaded documents are **optional** and validated client-side; this demo does not persist files."
    )

# Load DB early and validate schema
try:
    df, norm_map, data_path = load_db()
except Exception as e:
    st.error(f"Data load error: {e}")
    st.stop()

required_cols = [
    "roll number", "first name and middle name", "last name", "program", "semester", "section",
    "sms phone number", "student email", "father's name", "father mobile no.", "father email",
    "mother's name", "mother mobile no."
]
# Some source columns may have trailing spaces or different case -> normalized mapping handles it.
try:
    require_columns(norm_map, required_cols)
except KeyError as e:
    st.error(str(e))
    st.stop()

# -----------------------------
# Inputs
# -----------------------------
with st.form("outgate_form", clear_on_submit=False):
    st.subheader("Request Details")

    email = st.text_input(
        "Student Email",
        placeholder="student.name@woxsen.edu.in",
        help="Must match an email present in the student master dataset.",
    )

    col1, col2 = st.columns(2)
    default_start = TODAY
    default_end = TODAY
    max_allowed_date = TODAY + timedelta(days=MAX_FUTURE_DAYS)

    with col1:
        start_date = st.date_input(
            "From (inclusive)",
            value=default_start,
            min_value=TODAY,
            max_value=max_allowed_date,
            help=f"Must be between today and {max_allowed_date.isoformat()}."
        )
    with col2:
        end_date = st.date_input(
            "To (inclusive)",
            value=default_end,
            min_value=TODAY,
            max_value=max_allowed_date,
            help=f"Must not exceed {MAX_DURATION_DAYS} days and cannot be before the start date."
        )

    reason = st.text_area(
        "Reason for out-gate",
        max_chars=MAX_REASON_LEN,
        placeholder="e.g., Medical appointment, family emergency, competition participation, etc.",
        help=f"Be specific. {MIN_REASON_LEN}-{MAX_REASON_LEN} characters."
    )

    uploaded = st.file_uploader(
        "Optional supporting document",
        type=ALLOWED_FILE_TYPES,
        accept_multiple_files=False,
        help=f"Accepted: {', '.join(ALLOWED_FILE_TYPES).upper()} • Max {MAX_UPLOAD_MB} MB"
    )

    # Primary form action
    submitted = st.form_submit_button("Validate & Preview", type="primary")

# -----------------------------
# Validation & Lookup
# -----------------------------
valid = False
student_row = None
errors = []

if submitted:
    # Email checks
    if not validate_email(email):
        errors.append("Enter a valid email address.")
    else:
        # Lookup in CSV
        matches = find_student_by_email(df, norm_map, email)
        if matches.empty:
            errors.append("No student found for the provided email.")
        elif len(matches) > 1:
            st.warning("Multiple records found for this email. Using the first match.")
            student_row = matches.iloc[0]
        else:
            student_row = matches.iloc[0]

    # Date checks
    if end_date < start_date:
        errors.append("End date cannot be before start date.")
    duration_days = (end_date - start_date).days + 1  # inclusive
    if duration_days > MAX_DURATION_DAYS:
        errors.append(f"Requested duration is {duration_days} days. The maximum allowed is {MAX_DURATION_DAYS} days.")
    if start_date < TODAY or end_date < TODAY:
        errors.append("Dates cannot be in the past.")
    if end_date > TODAY + timedelta(days=MAX_FUTURE_DAYS):
        errors.append(f"End date cannot be later than {MAX_FUTURE_DAYS} days from today.")

    # Reason checks
    if not reason or len(reason.strip()) < MIN_REASON_LEN:
        errors.append(f"Reason must be at least {MIN_REASON_LEN} characters.")
    if reason and any(ord(ch) < 9 for ch in reason):
        errors.append("Reason contains invalid control characters.")

    # File checks
    upload_info = None
    if uploaded is not None:
        # Size
        uploaded.seek(0, io.SEEK_END)
        size = uploaded.tell()
        uploaded.seek(0)
        if size > MAX_UPLOAD_MB * 1024 * 1024:
            errors.append(f"Uploaded file exceeds {MAX_UPLOAD_MB} MB.")
        # Extension already filtered by Streamlit's type=, but double-check
        name = uploaded.name or ""
        ext = name.split(".")[-1].lower() if "." in name else ""
        if ext not in ALLOWED_FILE_TYPES:
            errors.append("Unsupported file type uploaded.")
        # Hash for integrity (without persisting the document)
        content = uploaded.read()
        uploaded.seek(0)
        upload_info = {
            "name": name,
            "size": size,
            "sha256": sha256_of_bytes(content),
        }

    if errors:
        st.error("\n".join(f"• {e}" for e in errors))
    else:
        valid = True

# -----------------------------
# Preview Section
# -----------------------------
if valid and student_row is not None:
    st.success("All checks passed. Review the details below and submit your request.")

    # Extract fields, using normalized map to reference real columns safely
    def gv(key_norm: str) -> str:
        return str(student_row.get(norm_map[key_norm], "")).strip()

    # Build student info (mask sensitive phone)
    student_info = {
        "Roll Number": gv("roll number"),
        "Name": (gv("first name and middle name") + " " + gv("last name")).strip(),
        "Program": gv("program"),
        "Semester": gv("semester"),
        "Section": gv("section"),
        "SMS Phone": mask_phone(gv("sms phone number")),
        "Student Email": gv("student email"),
    }

    parent_info = {
        "Father's Name": gv("father's name"),
        "Father Mobile": mask_phone(gv("father mobile no.")),
        "Father Email": mask_email(gv("father email")),
        "Mother's Name": gv("mother's name"),
        "Mother Mobile": mask_phone(gv("mother mobile no.")),
    }

    # Request details
    ts = datetime.now(INDIA_TZ)
    request_meta = {
        "From": start_date.isoformat(),
        "To": end_date.isoformat(),
        "Duration (days)": (end_date - start_date).days + 1,
        "Reason": reason.strip(),
        "Document": upload_info["name"] if uploaded is not None else "—",
        "Request Timestamp": ts.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }

    st.subheader("Student Basic Information")
    st.table(pd.DataFrame(student_info.items(), columns=["Field", "Value"]))

    st.subheader("Parent Contact Details")
    st.table(pd.DataFrame(parent_info.items(), columns=["Field", "Value"]))

    st.subheader("Request Summary")
    st.table(pd.DataFrame(request_meta.items(), columns=["Field", "Value"]))

    # Submission button (separate to avoid accidental writes)
    if st.button("Submit Out-Gate Request", type="primary"):
        # Build log entry (with minimal PII; keep full student email to identify the requester)
        log_entry = {
            "timestamp": ts.isoformat(),
            "student_email": student_info["Student Email"],
            "roll_number": student_info["Roll Number"],
            "program": student_info["Program"],
            "semester": student_info["Semester"],
            "section": student_info["Section"],
            "from_date": request_meta["From"],
            "to_date": request_meta["To"],
            "duration_days": request_meta["Duration (days)"],
            "reason": reason.strip()[:MAX_REASON_LEN],
            "upload_name": upload_info["name"] if uploaded is not None else "",
            "upload_sha256": upload_info["sha256"] if uploaded is not None else "",
            "status": "Submitted",
            "data_source": os.path.basename(find_data_path() or ""),
        }
        try:
            append_log(log_entry)

            st.success("✅ Request submitted successfully.")
            if ENABLE_LOGGING:
                st.caption(f"Request recorded in: `{LOG_PATH}` (server-side).")
        except Exception as e:
            st.error(f"Failed to record request: {e}")

# Footer
st.markdown("---")
st.caption(
    "Privacy: This demo masks sensitive information in the UI and avoids persisting uploaded documents. "
    "Ensure proper access controls and transport security (HTTPS) in production deployments."
)

# import os
# import io
# import csv
# import uuid
# import hashlib
# import secrets
# import sqlite3
# import smtplib
# import email.message
# import socket
# import traceback
# from contextlib import contextmanager
# from datetime import datetime, date, timedelta, timezone

# import pandas as pd
# import streamlit as st

# # ==============================
# # Config & constants
# # ==============================

# IST = timezone(timedelta(hours=5, minutes=30))
# DB_PATH = os.getenv("DB_PATH", "leave.db")
# STUDENTS_CSV_PATH = os.getenv("STUDENTS_CSV_PATH", "students_master_data.csv")

# MAX_REASON_LEN = 500
# MAX_LEAVE_DAYS = 14
# TOKEN_TTL_HOURS = 24
# ALLOWED_DOC_EXTS = {".pdf", ".png", ".jpg", ".jpeg"}

# # Secrets (with sane defaults for local runs)
# ADMIN_EMAIL = st.secrets.get("ADMIN_EMAIL", "admin@woxsen.edu.in")
# SECURITY_EMAIL = st.secrets.get("SECURITY_EMAIL", "security@woxsen.edu.in")
# PUBLIC_BASE_URL = st.secrets.get("PUBLIC_BASE_URL", "http://localhost:8501").rstrip("/")
# SMTP_SECURITY = (st.secrets.get("SMTP_SECURITY", "auto") or "auto").lower()


# SMTP_HOST = st.secrets.get("SMTP_HOST")
# SMTP_PORT = int(st.secrets.get("SMTP_PORT", 465)) if st.secrets.get("SMTP_PORT") else None
# SMTP_USER = st.secrets.get("SMTP_USER")
# SMTP_PASS = st.secrets.get("SMTP_PASS")
# SMTP_FROM = st.secrets.get("SMTP_FROM", ADMIN_EMAIL)


# SMTP_HOST_OVERRIDE_IP = (st.secrets.get("SMTP_HOST_OVERRIDE_IP") or "").strip() or None


# # ==============================
# # Utility helpers
# # ==============================

# def _sha256(s: str) -> str:
#     return hashlib.sha256(s.encode()).hexdigest()

# def mask_email(addr: str) -> str:
#     if not addr or "@" not in addr:
#         return addr or ""
#     name, domain = addr.split("@", 1)
#     if len(name) <= 2:
#         masked = name[0] + "*"
#     else:
#         masked = name[0] + "*" * (len(name)-2) + name[-1]
#     return f"{masked}@{domain}"

# def mask_phone(ph: str) -> str:
#     if not ph:
#         return ""
#     digits = "".join([c for c in ph if c.isdigit()])
#     if len(digits) < 4:
#         return "*" * len(digits)
#     return "*" * (len(digits) - 4) + digits[-4:]

# def ext_ok(filename: str) -> bool:
#     if not filename:
#         return False
#     ext = os.path.splitext(filename)[1].lower()
#     return ext in ALLOWED_DOC_EXTS

# @st.cache_data(show_spinner=False)
# def load_students_csv(path: str) -> pd.DataFrame:
#     try:
#         df = pd.read_csv(path)
#         # normalize columns (case-insensitive)
#         df.columns = [c.strip() for c in df.columns]
#         return df
#     except Exception as e:
#         st.error(f"Failed to read students_master_data.csv: {e}")
#         return pd.DataFrame()

# def ci_get(row: pd.Series, options: list[str], default=""):
#     # case-insensitive lookup for one of possible column names
#     cols = {c.lower(): c for c in row.index}
#     for opt in options:
#         if opt.lower() in cols:
#             return row[cols[opt.lower()]]
#     return default

# # ==============================
# # Database helpers
# # ==============================

# @contextmanager
# def db():
#     con = sqlite3.connect(DB_PATH, isolation_level=None)
#     con.row_factory = sqlite3.Row
#     con.execute("""
#     CREATE TABLE IF NOT EXISTS leave_applications (
#       application_id TEXT PRIMARY KEY,
#       status TEXT NOT NULL,               -- PENDING | APPROVED | REJECTED
#       submitted_at TEXT NOT NULL,
#       from_date TEXT NOT NULL,
#       to_date TEXT NOT NULL,
#       reason TEXT NOT NULL,
#       reason_type TEXT,
#       doc_name TEXT,
#       doc_sha256 TEXT,

#       student_email TEXT NOT NULL,
#       student_name TEXT NOT NULL,
#       program TEXT,
#       semester TEXT,
#       section TEXT,

#       father_name TEXT,
#       father_mobile TEXT,
#       father_email TEXT,
#       mother_name TEXT,
#       mother_mobile TEXT,

#       approve_token_hash TEXT NOT NULL,
#       reject_token_hash  TEXT NOT NULL,
#       token_expires_at TEXT NOT NULL,

#       decided_at TEXT,
#       decided_by TEXT
#     );
#     """)
#     con.execute("""
#     CREATE TABLE IF NOT EXISTS notifications_log (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       application_id TEXT NOT NULL,
#       channel TEXT NOT NULL,
#       recipient TEXT NOT NULL,
#       subject TEXT NOT NULL,
#       sent_at TEXT NOT NULL,
#       status TEXT NOT NULL,
#       error TEXT
#     );
#     """)
#     try:
#         yield con
#     finally:
#         con.close()

# def insert_application(payload: dict, approve_hash: str, reject_hash: str, exp_iso: str):
#     now_iso = datetime.now(IST).isoformat()
#     with db() as con:
#         con.execute("BEGIN IMMEDIATE")
#         con.execute("""
#             INSERT INTO leave_applications (
#               application_id, status, submitted_at, from_date, to_date, reason, reason_type,
#               doc_name, doc_sha256, student_email, student_name, program, semester, section,
#               father_name, father_mobile, father_email, mother_name, mother_mobile,
#               approve_token_hash, reject_token_hash, token_expires_at
#             ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
#         """, (
#             payload["application_id"], "PENDING", now_iso,
#             payload["from_date"], payload["to_date"], payload["reason"], payload.get("reason_type"),
#             payload.get("doc_name"), payload.get("doc_sha256"),
#             payload["student_email"], payload["student_name"], payload.get("program"), payload.get("semester"), payload.get("section"),
#             payload.get("father_name"), payload.get("father_mobile"), payload.get("father_email"),
#             payload.get("mother_name"), payload.get("mother_mobile"),
#             approve_hash, reject_hash, exp_iso
#         ))

# def get_application(aid: str):
#     with db() as con:
#         row = con.execute("SELECT * FROM leave_applications WHERE application_id=?", (aid,)).fetchone()
#         return row

# def update_status(aid: str, new_status: str):
#     now_iso = datetime.now(IST).isoformat()
#     with db() as con:
#         con.execute("BEGIN IMMEDIATE")
#         con.execute("UPDATE leave_applications SET status=?, decided_at=?, decided_by=? WHERE application_id=?",
#                     (new_status, now_iso, ADMIN_EMAIL, aid))

# def log_email(application_id: str, channel: str, recipient: str, subject: str, status: str, error: str | None):
#     with db() as con:
#         con.execute("""
#             INSERT INTO notifications_log (application_id, channel, recipient, subject, sent_at, status, error)
#             VALUES (?, ?, ?, ?, ?, ?, ?)
#         """, (application_id, channel, recipient, subject, datetime.now(IST).isoformat(), status, error))

# # ==============================
# # Email sending
# # ==============================


# # ==============================
# # Email sending (hardened; returns (ok, err))
# # ==============================

# def send_html(to: str, subject: str, html: str, channel: str, application_id: str):
#     """
#     Returns (ok, error_message_or_None). Always logs to notifications_log.
#     """
#     if not SMTP_HOST or not SMTP_FROM:
#         msg = "Missing SMTP secrets; skipping send."
#         st.info(f"(DEV) {msg} Would send to {to}: {subject}")
#         log_email(application_id, channel, to, subject, "SKIPPED_NO_SMTP", None)
#         return False, msg

#     if "://" in SMTP_HOST:
#         err = f"SMTP_HOST must be a hostname, not a URL: {SMTP_HOST!r}"
#         st.error(err); log_email(application_id, channel, to, subject, "FAILED", err)
#         return False, err

#     msg_obj = email.message.EmailMessage()
#     msg_obj["From"] = SMTP_FROM
#     msg_obj["To"] = to
#     msg_obj["Subject"] = subject
#     msg_obj.set_content("This email requires an HTML-capable client.")
#     msg_obj.add_alternative(html, subtype="html")

#     try:
#         connect_host = SMTP_HOST_OVERRIDE_IP or SMTP_HOST  # TEMP override if needed

#         # DNS probe (raises on failure). Works for hostnames and IP literals.
#         socket.getaddrinfo(connect_host, SMTP_PORT)

#         use_starttls = (SMTP_SECURITY == "starttls") or (SMTP_SECURITY == "auto" and SMTP_PORT == 587)

#         if use_starttls:
#             with smtplib.SMTP(connect_host, SMTP_PORT, timeout=20) as s:
#                 s.ehlo(); s.starttls(); s.ehlo()
#                 if SMTP_USER and SMTP_PASS:
#                     s.login(SMTP_USER, SMTP_PASS)
#                 s.send_message(msg_obj)
#         else:
#             with smtplib.SMTP_SSL(connect_host, SMTP_PORT, timeout=20) as s:
#                 if SMTP_USER and SMTP_PASS:
#                     s.login(SMTP_USER, SMTP_PASS)
#                 s.send_message(msg_obj)

#         log_email(application_id, channel, to, subject, "SENT", None)
#         return True, None

#     except Exception as e:
#         tb = traceback.format_exc()
#         err = f"{e}"
#         st.error(f"Failed to send email to {to}: {err}")
#         st.caption(f"SMTP_HOST={SMTP_HOST!r} PORT={SMTP_PORT} SECURITY={SMTP_SECURITY} OVERRIDE={SMTP_HOST_OVERRIDE_IP!r}")
#         log_email(application_id, channel, to, subject, "FAILED", f"{err}\n{tb}")
#         return False, err

# # ==============================
# # Email templates (HTML)
# # ==============================

# HEADER = """\
# <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f9fc;padding:24px 0;">
#   <tr>
#     <td align="center">
#       <table role="presentation" width="640" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;">
#         <tr>
#           <td style="padding:16px 24px;background:#111827;color:#ffffff;font-family:Arial,Helvetica,sans-serif;">
#             <h2 style="margin:0;font-size:18px;line-height:24px;">Woxsen University • Leave Application System</h2>
#           </td>
#         </tr>
#         <!-- BODY STARTS HERE -->
# """

# FOOTER = """\
#         <!-- BODY ENDS HERE -->
#         <tr>
#           <td style="padding:16px 24px;background:#f9fafb;color:#6b7280;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;">
#             This is an automated message from the Leave Application System. If you didn’t expect this, you can ignore it.
#           </td>
#         </tr>
#       </table>
#     </td>
#   </tr>
# </table>
# """

# def tmpl_admin_review(ctx: dict) -> str:
#     doc_html = f'<div><b>Document:</b> <a href="{ctx["doc_url"]}">View</a></div>' if ctx.get("doc_url") else ""
#     return HEADER + f"""
# <tr><td style="padding:24px;font-family:Arial,Helvetica,sans-serif;color:#111827;">
#   <p style="margin:0 0 12px;">Dear Admin,</p>
#   <p style="margin:0 0 16px;">A new leave application has been submitted. Please review the details below and select an action.</p>

#   <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;margin:0 0 16px;">
#     <tr><td style="padding:12px 16px;">
#       <div><b>Name:</b> {ctx["student_name"]}</div>
#       <div><b>Email:</b> {ctx["student_email"]}</div>
#       <div><b>Course:</b> {ctx.get("program","-")}</div>
#       <div><b>Leave Dates:</b> {ctx["from_date"]} – {ctx["to_date"]}</div>
#       <div><b>Reason:</b> {ctx["reason"]}</div>
#       {doc_html}
#       <div><b>Application ID:</b> {ctx["application_id"]}</div>
#     </td></tr>
#   </table>

#   <p style="margin:0 0 12px;"><i>For security, you’ll confirm this action on the site.</i></p>

#   <div style="margin:18px 0;">
#     <a href="{ctx["base_url"]}/?aid={ctx["application_id"]}&action=approve&t={ctx["approve_token"]}"
#        style="background:#059669;color:#ffffff;text-decoration:none;padding:12px 18px;border-radius:6px;display:inline-block;font-weight:bold;">
#        Approve
#     </a>
#     <span style="display:inline-block;width:12px;"></span>
#     <a href="{ctx["base_url"]}/?aid={ctx["application_id"]}&action=reject&t={ctx["reject_token"]}"
#        style="background:#dc2626;color:#ffffff;text-decoration:none;padding:12px 18px;border-radius:6px;display:inline-block;font-weight:bold;">
#        Reject
#     </a>
#   </div>
# </td></tr>
# """ + FOOTER

# def tmpl_admin_confirm(ctx: dict) -> str:
#     return HEADER + f"""
# <tr><td style="padding:24px;font-family:Arial,Helvetica,sans-serif;color:#111827;">
#   <p style="margin:0 0 12px;">Dear Admin,</p>
#   <p style="margin:0 0 16px;">Your decision for the leave application below has been processed.</p>
#   <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;margin:0 0 16px;">
#     <tr><td style="padding:12px 16px;">
#       <div><b>Status:</b> {ctx["status"]}</div>
#       <div><b>Name:</b> {ctx["student_name"]}</div>
#       <div><b>Dates:</b> {ctx["from_date"]} – {ctx["to_date"]}</div>
#       <div><b>Application ID:</b> {ctx["application_id"]}</div>
#       <div><b>Processed At:</b> {ctx["processed_at"]} (Asia/Kolkata)</div>
#     </td></tr>
#   </table>
#   <p style="margin:0;">Regards,<br/>Leave Application System</p>
# </td></tr>
# """ + FOOTER

# def tmpl_security_approved(ctx: dict) -> str:
#     return HEADER + f"""
# <tr><td style="padding:24px;font-family:Arial,Helvetica,sans-serif;color:#111827;">
#   <p style="margin:0 0 12px;">Dear Security Team,</p>
#   <p style="margin:0 0 12px;">Please note the approved leave below:</p>

#   <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;margin:0 0 16px;">
#     <tr><td style="padding:12px 16px;">
#       <div><b>Student:</b> {ctx["student_name"]} ({ctx["student_email"]})</div>
#       <div><b>Course:</b> {ctx.get("program","-")}</div>
#       <div><b>Leave Window:</b> {ctx["from_date"]} – {ctx["to_date"]}</div>
#       <div><b>Reason:</b> {ctx["reason"]}</div>
#       <div><b>Parent Contact:</b> {ctx.get("parent_name","-")} • {ctx.get("parent_email","-")} • {ctx.get("parent_mobile","-")}</div>
#       <div><b>Application ID:</b> {ctx["application_id"]}</div>
#     </td></tr>
#   </table>

#   <p style="margin:0;">Please arrange access accordingly during this window.</p>
# </td></tr>
# """ + FOOTER

# def tmpl_parent_approved(ctx: dict) -> str:
#     return HEADER + f"""
# <tr><td style="padding:24px;font-family:Arial,Helvetica,sans-serif;color:#111827;">
#   <p style="margin:0 0 12px;">Dear {ctx.get("parent_name","Parent")},</p>
#   <p style="margin:0 0 12px;">We’re writing to inform you that {ctx["student_name"]}’s leave request has been <b>approved</b>.</p>
#   <ul style="margin:0 0 16px;padding-left:18px;">
#     <li><b>Dates:</b> {ctx["from_date"]} – {ctx["to_date"]}</li>
#     <li><b>Reason:</b> {ctx["reason"]}</li>
#     <li><b>Application ID:</b> {ctx["application_id"]}</li>
#   </ul>
#   <p style="margin:0;">Regards,<br/>Woxsen University</p>
# </td></tr>
# """ + FOOTER

# def tmpl_parent_rejected(ctx: dict) -> str:
#     note = f'<p style="margin:0 0 12px;"><b>Note:</b> {ctx["rejection_note"]}</p>' if ctx.get("rejection_note") else ""
#     return HEADER + f"""
# <tr><td style="padding:24px;font-family:Arial,Helvetica,sans-serif;color:#111827;">
#   <p style="margin:0 0 12px;">Dear {ctx.get("parent_name","Parent")},</p>
#   <p style="margin:0 0 12px;">We’re writing to inform you that {ctx["student_name"]}’s leave request has been <b>rejected</b>.</p>
#   <ul style="margin:0 0 16px;padding-left:18px;">
#     <li><b>Dates:</b> {ctx["from_date"]} – {ctx["to_date"]}</li>
#     <li><b>Reason:</b> {ctx["reason"]}</li>
#     <li><b>Application ID:</b> {ctx["application_id"]}</li>
#   </ul>
#   {note}
#   <p style="margin:0;">Regards,<br/>Woxsen University</p>
# </td></tr>
# """ + FOOTER

# def tmpl_student_approved(ctx: dict) -> str:
#     doc = f'<p style="margin:0 0 12px;">Document on file: <a href="{ctx["doc_url"]}">View</a></p>' if ctx.get("doc_url") else ""
#     return HEADER + f"""
# <tr><td style="padding:24px;font-family:Arial,Helvetica,sans-serif;color:#111827;">
#   <p style="margin:0 0 12px;">Dear {ctx["student_name"]},</p>
#   <p style="margin:0 0 12px;">Your leave request has been <b>approved</b>.</p>
#   <ul style="margin:0 0 16px;padding-left:18px;">
#     <li><b>Dates:</b> {ctx["from_date"]} – {ctx["to_date"]}</li>
#     <li><b>Reason:</b> {ctx["reason"]}</li>
#     <li><b>Application ID:</b> {ctx["application_id"]}</li>
#   </ul>
#   {doc}
#   <p style="margin:0;">Regards,<br/>Woxsen University</p>
# </td></tr>
# """ + FOOTER

# def tmpl_student_rejected(ctx: dict) -> str:
#     note = f'<p style="margin:0 0 12px;"><b>Note:</b> {ctx["rejection_note"]}</p>' if ctx.get("rejection_note") else ""
#     return HEADER + f"""
# <tr><td style="padding:24px;font-family:Arial,Helvetica,sans-serif;color:#111827;">
#   <p style="margin:0 0 12px;">Dear {ctx["student_name"]},</p>
#   <p style="margin:0 0 12px;">Your leave request has been <b>rejected</b>.</p>
#   <ul style="margin:0 0 16px;padding-left:18px;">
#     <li><b>Dates:</b> {ctx["from_date"]} – {ctx["to_date"]}</li>
#     <li><b>Reason:</b> {ctx["reason"]}</li>
#     <li><b>Application ID:</b> {ctx["application_id"]}</li>
#   </ul>
#   {note}
#   <p style="margin:0;">Regards,<br/>Woxsen University</p>
# </td></tr>
# """ + FOOTER

# # ==============================
# # Business logic
# # ==============================

# def send_admin_review_email(payload: dict, approve_token: str, reject_token: str):
#     ctx = {
#         "base_url": "http://localhost:8501",
#         "application_id": payload["application_id"],
#         "student_name": payload["student_name"],
#         "student_email": payload["student_email"],
#         "program": payload.get("program","-"),
#         "from_date": payload["from_date"],
#         "to_date": payload["to_date"],
#         "reason": payload["reason"],
#         "doc_url": payload.get("doc_url",""),
#         "approve_token": approve_token,
#         "reject_token": reject_token,
#     }
#     subject = f"New Leave Application – {ctx['student_name']} ({ctx['from_date']} to {ctx['to_date']})"
#     html = tmpl_admin_review(ctx)
#     send_html(ADMIN_EMAIL, subject, html, "admin", payload["application_id"])

# def send_decision_notifications(a_row: sqlite3.Row, status: str, rejection_note: str | None = None):
#     processed_at = datetime.now(IST).strftime("%B %d, %Y %I:%M %p")
#     ctx = {
#         "status": status,
#         "application_id": a_row["application_id"],
#         "student_name": a_row["student_name"],
#         "student_email": a_row["student_email"],
#         "program": a_row["program"] or "-",
#         "from_date": a_row["from_date"],
#         "to_date": a_row["to_date"],
#         "reason": a_row["reason"],
#         "doc_url": "",  # if you host docs, populate here
#         "processed_at": processed_at,
#         "parent_name": a_row["father_name"] or a_row["mother_name"] or "Parent",
#         "parent_email": a_row["father_email"] or "",
#         "parent_mobile": a_row["father_mobile"] or a_row["mother_mobile"] or "",
#         "rejection_note": rejection_note or "",
#     }
#     # Admin confirmation
#     send_html(ADMIN_EMAIL,
#               f"Leave Application {status} – {ctx['student_name']} ({ctx['from_date']} to {ctx['to_date']})",
#               tmpl_admin_confirm(ctx), "admin_confirm", a_row["application_id"])
#     if status == "APPROVED":
#         # Security
#         send_html(SECURITY_EMAIL,
#                   f"Approved Leave – {ctx['student_name']} ({ctx['from_date']} to {ctx['to_date']})",
#                   tmpl_security_approved(ctx), "security", a_row["application_id"])
#         # Parent
#         if ctx["parent_email"]:
#             send_html(ctx["parent_email"],
#                       f"Leave Approved – {ctx['student_name']} ({ctx['from_date']} to {ctx['to_date']})",
#                       tmpl_parent_approved(ctx), "parent", a_row["application_id"])
#         # Student
#         send_html(ctx["student_email"],
#                   f"Your Leave is Approved – {ctx['from_date']} to {ctx['to_date']}",
#                   tmpl_student_approved(ctx), "student", a_row["application_id"])
#     else:
#         # Parent
#         if ctx["parent_email"]:
#             send_html(ctx["parent_email"],
#                       f"Leave Decision – {ctx['student_name']} ({ctx['from_date']} to {ctx['to_date']})",
#                       tmpl_parent_rejected(ctx), "parent", a_row["application_id"])
#         # Student
#         send_html(ctx["student_email"],
#                   "Your Leave Request – Decision",
#                   tmpl_student_rejected(ctx), "student", a_row["application_id"])

# def process_action(aid: str, token: str, action: str) -> str:
#     now = datetime.now(IST)
#     with db() as con:
#         con.execute("BEGIN IMMEDIATE")
#         row = con.execute("""
#             SELECT status, token_expires_at, approve_token_hash, reject_token_hash
#             FROM leave_applications WHERE application_id=?
#         """, (aid,)).fetchone()
#         if not row:
#             # Log the issue to see the aid value
#             st.error(f"Application not found for application_id: {aid}")
#             return "Application not found."
#         if row["status"] in ("APPROVED","REJECTED"):
#             return "This leave application has already been processed."
#         if now > datetime.fromisoformat(row["token_expires_at"]):
#             return "This action has already been processed or the token has expired."

#         expected = row["approve_token_hash"] if action == "approve" else row["reject_token_hash"]
#         if _sha256(token) != expected:
#             return "This action has already been processed or the token has expired."

#         new_status = "APPROVED" if action == "approve" else "REJECTED"
#         con.execute("UPDATE leave_applications SET status=?, decided_at=?, decided_by=? WHERE application_id=?",
#                     (new_status, now.isoformat(), ADMIN_EMAIL, aid))
#         # reload for notifications
#         a_row = con.execute("SELECT * FROM leave_applications WHERE application_id=?", (aid,)).fetchone()

#     # Send notifications after commit
#     send_decision_notifications(a_row, new_status)
#     return f"Leave application {new_status.lower()}."

# # ==============================
# # UI: Approval confirmation route
# # ==============================

# def approval_route():
#     # Streamlit changed API for query params over time; try both
#     try:
#         params = st.query_params  # modern
#     except Exception:
#         params = st.experimental_get_query_params()  # legacy

#     def _first(v):
#         if v is None:
#             return None
#         if isinstance(v, list):
#             return v[0] if v else None
#         return v

#     aid = _first(params.get("aid"))
#     action = _first(params.get("action"))
#     token = _first(params.get("t"))

#     if not (aid and action in ("approve","reject") and token):
#         st.error("Invalid query parameters.")
#         return False  # not on approval route

#     st.header(f"{action.title()} Leave Application")
#     st.info("For security, please confirm this action.")
#     # Optional: allow rejection note
#     rejection_note = ""
#     if action == "reject":
#         rejection_note = st.text_area("Optional note to include in the rejection emails (not required):", "")

#     if st.button(f"Confirm {action.title()}"):
#         msg = process_action(aid, token, action)
#         st.success(msg)
#         st.stop()
#     st.stop()
#     return True

# # ==============================
# # UI: Student submission form
# # ==============================

# def date_to_text(d: date) -> str:
#     return d.strftime("%B %d, %Y")

# def generate_numeric_id_from_uuid():
#     # Generate a UUID
#     new_uuid = uuid.uuid4()
    
#     # Hash the UUID to create a more compact numeric ID
#     hashed_uuid = hashlib.sha256(new_uuid.bytes).hexdigest()
    
#     # Take the first 16 characters of the hash and convert to integer
#     numeric_id = int(hashed_uuid[:16], 16)
    
#     return str(numeric_id)

# def submission_form():
#     st.title("Out-Gate Leave Application")

#     df = load_students_csv(STUDENTS_CSV_PATH)

#     st.subheader("Student Details")
#     student_email_input = st.text_input("Student Email", key="student_email_input", placeholder="john.doe@student.woxsen.edu.in")

#     student_row = None
#     if student_email_input and not df.empty:
#         # case-insensitive match on email column(s)
#         email_cols = [c for c in df.columns if "email" in c.lower()]
#         if email_cols:
#             mask = False
#             for c in email_cols:
#                 mask = mask | (df[c].astype(str).str.lower() == student_email_input.strip().lower())
#             matches = df[mask]
#             if not matches.empty:
#                 student_row = matches.iloc[0]

#     if student_row is None:
#         st.caption("Enter your university email to auto-fill your details from master data.")
#     else:
#         # Extract fields with flexible names
#         student_name = ci_get(student_row, ["Name","Student Name","Full Name"], "")
#         program = ci_get(student_row, ["Program","Course"], "")
#         semester = ci_get(student_row, ["Semester"], "")
#         section = ci_get(student_row, ["Section"], "")
#         father_name = ci_get(student_row, ["Father's Name","Father Name"], "")
#         father_mobile = ci_get(student_row, ["Father Mobile No.","Father Mobile","Father Phone"], "")
#         father_email = ci_get(student_row, ["Father Email","Parent Email"], "")
#         mother_name = ci_get(student_row, ["Mother's Name","Mother Name"], "")
#         mother_mobile = ci_get(student_row, ["Mother Mobile No.","Mother Mobile","Mother Phone"], "")

#         st.write(f"**Name:** {student_name or '—'}")
#         st.write(f"**Course:** {program or '—'}")
#         # Mask sensitive contact info in UI
#         if father_email or father_mobile:
#             st.write(f"**Parent on file:** {father_name or '—'} • {mask_email(father_email) if father_email else ''} • {mask_phone(father_mobile) if father_mobile else ''}")

#     st.subheader("Leave Details")
#     col1, col2 = st.columns(2)
#     with col1:
#         from_dt = st.date_input("From (inclusive)", min_value=date.today())
#     with col2:
#         to_dt = st.date_input("To (inclusive)", min_value=from_dt if 'from_dt' in locals() else date.today())

#     reason = st.text_area("Reason", help="Be concise; 1–2 lines are sufficient.", max_chars=MAX_REASON_LEN)
#     upload = st.file_uploader("Optional Supporting Document (PDF/PNG/JPG)", type=[e.strip(".") for e in ALLOWED_DOC_EXTS])

#     # Submission button
#     if st.button("Submit Application"):
#         # Validations
#         if not student_email_input:
#             st.error("Student Email is required.")
#             return
#         if not student_row is not None:
#             st.error("Email not found in master data. Please check and try again.")
#             return
#         if from_dt > to_dt:
#             st.error("From date must be on or before To date.")
#             return
#         if from_dt < date.today():
#             st.error("Leave must start today or in the future.")
#             return
#         duration = (to_dt - from_dt).days + 1
#         if duration > MAX_LEAVE_DAYS:
#             st.error(f"Leave duration cannot exceed {MAX_LEAVE_DAYS} days (requested {duration}).")
#             return
#         if not reason or not reason.strip():
#             st.error("Reason is required.")
#             return

#         reason_type = "MEDICAL" if "medical" in reason.lower() else "OTHER"
#         if reason_type == "MEDICAL" and upload is None:
#             st.error("Medical leave requires a supporting document (PDF/PNG/JPG).")
#             return
#         if upload is not None and not ext_ok(upload.name):
#             st.error("Unsupported file type. Please upload PDF/PNG/JPG.")
#             return

#         # Prepare payload
#         application_id = generate_numeric_id_from_uuid()
#         doc_name = None
#         doc_sha256 = None
#         if upload is not None:
#             data = upload.read()
#             doc_sha256 = hashlib.sha256(data).hexdigest()
#             doc_name = upload.name
#             # (Optional) persist file to local storage if desired:
#             # save_dir = os.getenv("DOCS_DIR", "docs")
#             # os.makedirs(save_dir, exist_ok=True)
#             # with open(os.path.join(save_dir, f"{application_id}_{doc_name}"), "wb") as f:
#             #     f.write(data)

#         payload = {
#             "application_id": application_id,
#             "from_date": date_to_text(from_dt),
#             "to_date": date_to_text(to_dt),
#             "reason": reason.strip(),
#             "reason_type": reason_type,
#             "doc_name": doc_name,
#             "doc_sha256": doc_sha256,
#             "doc_url": "",  # if you host uploaded docs, put the URL here

#             "student_email": student_email_input.strip(),
#             "student_name": ci_get(student_row, ["Name","Student Name","Full Name"], ""),
#             "program": ci_get(student_row, ["Program","Course"], ""),
#             "semester": ci_get(student_row, ["Semester"], ""),
#             "section": ci_get(student_row, ["Section"], ""),

#             "father_name": ci_get(student_row, ["Father's Name","Father Name"], ""),
#             "father_mobile": ci_get(student_row, ["Father Mobile No.","Father Mobile","Father Phone"], ""),
#             "father_email": ci_get(student_row, ["Father Email","Parent Email"], ""),
#             "mother_name": ci_get(student_row, ["Mother's Name","Mother Name"], ""),
#             "mother_mobile": ci_get(student_row, ["Mother Mobile No.","Mother Mobile","Mother Phone"], ""),
#         }

#         # Tokens + DB insert
#         approve_token = secrets.token_urlsafe(32)
#         reject_token = secrets.token_urlsafe(32)
#         exp = datetime.now(IST) + timedelta(hours=TOKEN_TTL_HOURS)

#         insert_application(payload, _sha256(approve_token), _sha256(reject_token), exp.isoformat())

#         # Email admin
#         send_admin_review_email(payload, approve_token, reject_token)

#         st.success("✅ Application submitted. Admin has been notified via email.")
#         st.info(f"Your Application ID: **{application_id}**")

# # ==============================
# # Main
# # ==============================

# def main():
#     st.set_page_config(page_title="Woxsen Out-Gate Leave", page_icon="📝", layout="centered")

#     # Route: approval confirmation if query params present
#     if approval_route():
#         return

#     # Otherwise: show submission form
#     submission_form()

# if __name__ == "__main__":
#     main()
