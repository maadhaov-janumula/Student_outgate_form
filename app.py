
# test.py
import os
import io
import csv
import uuid
import hashlib
import secrets
import sqlite3
import smtplib
import email.message
from email.utils import make_msgid
import socket
import traceback
from contextlib import contextmanager
from datetime import datetime, date, timedelta, timezone

import pandas as pd
import streamlit as st
import mimetypes

# ==============================
# Config & constants
# ==============================

IST = timezone(timedelta(hours=5, minutes=30))
DB_PATH = os.getenv("DB_PATH", "leave.db")
STUDENTS_CSV_PATH = os.getenv("STUDENTS_CSV_PATH", "test_data_SOG.xlsx")

MAX_ATTACHMENT_BYTES = int(os.getenv("MAX_ATTACHMENT_BYTES", str(8 * 1024 * 1024)))  # 8 MB default
MAX_REASON_LEN = 500
MAX_LEAVE_DAYS = 14
TOKEN_TTL_HOURS = 24
ALLOWED_DOC_EXTS = {".pdf", ".png", ".jpg", ".jpeg"}

# Secrets (with sane defaults for local runs)
ADMIN_EMAIL = st.secrets.get("ADMIN_EMAIL", "admin@woxsen.edu.in")
SECURITY_EMAIL = st.secrets.get("SECURITY_EMAIL", "security@woxsen.edu.in")
PUBLIC_BASE_URL = st.secrets.get("PUBLIC_BASE_URL", "http://localhost:8501").rstrip("/")
SMTP_SECURITY = (st.secrets.get("SMTP_SECURITY", "auto") or "auto").lower()

SMTP_HOST = st.secrets.get("SMTP_HOST")
SMTP_PORT = int(st.secrets.get("SMTP_PORT", 465))
SMTP_USER = st.secrets.get("SMTP_USER")
SMTP_PASS = st.secrets.get("SMTP_PASS")
SMTP_FROM = st.secrets.get("SMTP_FROM", ADMIN_EMAIL)

SMTP_HOST_OVERRIDE_IP = (st.secrets.get("SMTP_HOST_OVERRIDE_IP") or "").strip() or None

# Dev flag: enable extra captions/logging
DEV_MODE = bool(st.secrets.get("DEV_MODE", False) or os.getenv("DEV_MODE"))

# ==============================
# Utility helpers
# ==============================

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

def mask_email(addr: str) -> str:
    if not addr or "@" not in addr:
        return addr or ""
    name, domain = addr.split("@", 1)
    if len(name) <= 2:
        masked = name[0] + "*"
    else:
        masked = name[0] + "*" * (len(name)-2) + name[-1]
    return f"{masked}@{domain}"

def mask_phone(ph: str) -> str:
    if not ph:
        return ""
    digits = "".join([c for c in ph if c.isdigit()])
    if len(digits) < 4:
        return "*" * len(digits)
    return "*" * (len(digits) - 4) + digits[-4:]

def ext_ok(filename: str) -> bool:
    if not filename:
        return False
    ext = os.path.splitext(filename)[1].lower()
    return ext in ALLOWED_DOC_EXTS

@st.cache_data(show_spinner=False)
def load_students_csv(path: str) -> pd.DataFrame:
    try:
        # Read Excel as strings, normalize headers, and replace NaNs with ""
        df = pd.read_excel(path, dtype=str).fillna("")
        df.columns = [c.strip() for c in df.columns]
        return df
    except Exception as e:
        st.error(f"Failed to read Student Master List: {e}")
        return pd.DataFrame()

def ci_get(row: pd.Series, options: list[str], default=""):
    # case-insensitive lookup for one of possible column names
    cols = {c.lower(): c for c in row.index}
    for opt in options:
        if opt.lower() in cols:
            return row[cols[opt.lower()]]
    return default

def _normalize_space(s: str) -> str:
    import re
    return re.sub(r"\s+", " ", (s or "").strip())

def _name_from_email(email: str) -> str:
    import re
    if not email or "@" not in email:
        return ""
    local = email.split("@", 1)[0]
    local = re.sub(r"[\._\-]+", " ", local)
    return _normalize_space(local).title()

def get_student_name(row: pd.Series, fallback_email: str = "") -> str:
    """
    Build a name using the canonical Student Master List column first, then fall back
    to legacy variants and finally derive a best-effort name from the email address.
    """
    # Prefer the single-field name provided in the master list
    student_name = str(ci_get(row, ["Student Name"], "")).strip()
    if student_name:
        return _normalize_space(student_name)

    # Legacy multi-column variants
    first_middle = str(ci_get(row, ["First Name and Middle Name"], "")).strip()
    last = str(ci_get(row, ["Last Name"], "")).strip()
    if first_middle or last:
        return _normalize_space(f"{first_middle} {last}")

    # Single-field name variants from older extracts
    single_variants = ["Full Name", "Name", "Student_Name", "StudentName", "name"]
    name = str(ci_get(row, single_variants, "")).strip()
    if name:
        return _normalize_space(name)

    # Generic first/last variants
    first = str(ci_get(row, ["First Name", "First_Name", "FirstName", "Given Name", "GivenName", "Forename"], "")).strip()
    last = str(ci_get(row, ["Last Name", "Last_Name", "LastName", "Surname", "Family Name", "Family_Name"], "")).strip()
    if first or last:
        return _normalize_space(f"{first} {last}")

    # Fallback from email
    return _name_from_email(fallback_email)

# ==============================
# Database helpers
# ==============================

def _ensure_column(con: sqlite3.Connection, table: str, column: str, coltype: str):
    # Add a column if it doesn't exist
    cur = con.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    if column not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

@contextmanager
def db():
    # isolation_level=None => autocommit mode
    # Re-read DB_PATH from environment each call so tests can override after import
    _path = os.getenv('DB_PATH', DB_PATH)
    con = sqlite3.connect(_path, isolation_level=None, uri=_path.startswith('file:'))
    con.row_factory = sqlite3.Row
    con.execute("""
    CREATE TABLE IF NOT EXISTS leave_applications (
      application_id TEXT PRIMARY KEY,
      status TEXT NOT NULL,               -- PENDING | APPROVED | REJECTED
      submitted_at TEXT NOT NULL,
      from_date TEXT NOT NULL,
      to_date TEXT NOT NULL,
      reason TEXT NOT NULL,
      reason_type TEXT,
      doc_name TEXT,
      doc_sha256 TEXT,

      student_email TEXT NOT NULL,
      student_name TEXT NOT NULL,
      program TEXT,
      semester TEXT,
      section TEXT,

      father_name TEXT,
      father_mobile TEXT,
      father_email TEXT,
      mother_name TEXT,
      mother_email TEXT,
      mother_mobile TEXT,

      approve_token_hash TEXT NOT NULL,
      reject_token_hash  TEXT NOT NULL,
      token_expires_at TEXT NOT NULL,

      decided_at TEXT,
      decided_by TEXT
    );
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS notifications_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      application_id TEXT NOT NULL,
      channel TEXT NOT NULL,
      recipient TEXT NOT NULL,
      subject TEXT NOT NULL,
      sent_at TEXT NOT NULL,
      status TEXT NOT NULL,
      error TEXT
    );
    """)
    # ---- Lightweight migration: ensure new columns exist
    _ensure_column(con, "leave_applications", "mother_email", "TEXT")
    _ensure_column(con, "leave_applications", "admin_review_msgid", "TEXT")
    try:
        yield con
    finally:
        con.close()

def insert_application(payload: dict, approve_hash: str, reject_hash: str, exp_iso: str):
    """
    Insert application in autocommit mode and immediately verify persistence.
    Raises RuntimeError if the insert did not round-trip.
    """
    now_iso = datetime.now(IST).isoformat()
    with db() as con:
        # NO manual BEGIN here; rely on autocommit per statement.
        con.execute("""
            INSERT INTO leave_applications (
              application_id, status, submitted_at, from_date, to_date, reason, reason_type,
              doc_name, doc_sha256, student_email, student_name, program, semester, section,
              father_name, father_mobile, father_email, mother_name, mother_email, mother_mobile,
              approve_token_hash, reject_token_hash, token_expires_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            payload["application_id"], "PENDING", now_iso,
            payload["from_date"], payload["to_date"], payload["reason"], payload.get("reason_type"),
            payload.get("doc_name"), payload.get("doc_sha256"),
            payload["student_email"], payload["student_name"], payload.get("program"), payload.get("semester"), payload.get("section"),
            payload.get("father_name"), payload.get("father_mobile"), payload.get("father_email"),
            payload.get("mother_name"), payload.get("mother_email"), payload.get("mother_mobile"),
            approve_hash, reject_hash, exp_iso
        ))
        # Verify persistence immediately
        chk = con.execute(
            "SELECT application_id, status, submitted_at FROM leave_applications WHERE application_id=?",
            (payload["application_id"],)
        ).fetchone()
        if not chk or chk["application_id"] != payload["application_id"] or chk["status"] != "PENDING" or not chk["submitted_at"]:
            raise RuntimeError(f"Insert did not persist for application_id={payload['application_id']}")

def get_application(aid: str):
    with db() as con:
        row = con.execute("SELECT * FROM leave_applications WHERE application_id=?", (aid,)).fetchone()
        return row

def update_status(aid: str, new_status: str):
    now_iso = datetime.now(IST).isoformat()
    with db() as con:
        # NO manual BEGIN; rely on autocommit
        con.execute(
            "UPDATE leave_applications SET status=?, decided_at=?, decided_by=? WHERE application_id=?",
            (new_status, now_iso, ADMIN_EMAIL, aid)
        )

def log_email(application_id: str, channel: str, recipient: str, subject: str, status: str, error: str | None):
    with db() as con:
        con.execute("""
            INSERT INTO notifications_log (application_id, channel, recipient, subject, sent_at, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (application_id, channel, recipient, subject, datetime.now(IST).isoformat(), status, error))

# ==============================
# Email sending (now supports replies)
# ==============================

def _from_domain(addr: str) -> str:
    try:
        return (addr.split("@", 1)[1] or "").strip()
    except Exception:
        return ""

def send_html(to: str, subject: str, html: str, channel: str, application_id: str, headers: dict | None = None, attachments: list[tuple[str, bytes, str]] | None = None):
    """
    Returns (ok, error_message_or_None, message_id). Always logs to notifications_log.
    To thread as a reply, pass headers={"In-Reply-To": "<msgid>", "References": "<msgid>"}.
    To attach files, pass attachments=[(filename, bytes, mime), ...].
    """
    if not SMTP_HOST or not SMTP_FROM:
        msg = "Missing SMTP secrets; skipping send."
        st.info(f"(DEV) {msg} Would send to {to}: {subject}")
        log_email(application_id, channel, to, subject, "SKIPPED_NO_SMTP", None)
        return False, msg, None

    if "://" in SMTP_HOST:
        err = f"SMTP_HOST must be a hostname, not a URL: {SMTP_HOST!r}"
        st.error(err); log_email(application_id, channel, to, subject, "FAILED", err)
        return False, err, None

    msg_obj = email.message.EmailMessage()
    msg_obj["From"] = SMTP_FROM
    msg_obj["To"] = to
    msg_obj["Subject"] = subject
    # Generate/stamp a Message-ID so we can reference it later
    msg_domain = _from_domain(SMTP_FROM) or "woxsen.edu.in"
    msg_id = make_msgid(domain=msg_domain)
    msg_obj["Message-ID"] = msg_id

    # Apply any reply/thread headers
    headers = headers or {}
    for k, v in headers.items():
        if v:
            msg_obj[k] = v

    msg_obj.set_content("This email requires an HTML-capable client.")
    msg_obj.add_alternative(html, subtype="html")

    # Attachments (if any)
    for (fname, data, mime) in (attachments or []):
        maintype, subtype = (mime.split("/", 1) if "/" in mime else ("application", "octet-stream"))
        msg_obj.add_attachment(data, maintype=maintype, subtype=subtype, filename=fname)    

    try:
        connect_host = SMTP_HOST_OVERRIDE_IP or SMTP_HOST  # TEMP override if needed

        # DNS probe (raises on failure). Works for hostnames and IP literals.
        socket.getaddrinfo(connect_host, SMTP_PORT)

        use_starttls = (SMTP_SECURITY == "starttls") or (SMTP_SECURITY == "auto" and SMTP_PORT == 587)

        if use_starttls:
            with smtplib.SMTP(connect_host, SMTP_PORT, timeout=20) as s:
                s.ehlo(); s.starttls(); s.ehlo()
                if SMTP_USER and SMTP_PASS:
                    s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg_obj)
        else:
            with smtplib.SMTP_SSL(connect_host, SMTP_PORT, timeout=20) as s:
                if SMTP_USER and SMTP_PASS:
                    s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg_obj)

        log_email(application_id, channel, to, subject, "SENT", None)
        return True, None, msg_id

    except Exception as e:
        tb = traceback.format_exc()
        err = f"{e}"
        st.error(f"Failed to send email to {to}: {err}")
        st.caption(f"SMTP_HOST={SMTP_HOST!r} PORT={SMTP_PORT} SECURITY={SMTP_SECURITY} OVERRIDE={SMTP_HOST_OVERRIDE_IP!r}")
        log_email(application_id, channel, to, subject, "FAILED", f"{err}\n{tb}")
        return False, err, None

# ==============================
# Email templates (HTML)
# ==============================

HEADER = """\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f9fc;padding:24px 0;">
  <tr>
    <td align="center">
      <table role="presentation" width="640" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;">
        <tr>
          <td style="padding:16px 24px;background:#111827;color:#ffffff;font-family:Arial,Helvetica,sans-serif;">
            <h2 style="margin:0;font-size:18px;line-height:24px;">Woxsen University • Leave Application System</h2>
          </td>
        </tr>
        <!-- BODY STARTS HERE -->
"""

FOOTER = """\
        <!-- BODY ENDS HERE -->
        <tr>
          <td style="padding:16px 24px;background:#f9fafb;color:#6b7280;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;">
            This is an automated message from the Leave Application System. If you didn’t expect this, you can ignore it.
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
"""

def tmpl_admin_review(ctx: dict) -> str:
    doc_html = ""
    if ctx.get("doc_url"):
        doc_html = f'<div><b>Document:</b> <a href="{ctx["doc_url"]}">View</a></div>'
    elif ctx.get("doc_attached") and ctx.get("doc_name"):
        doc_html = f'<div><b>Document:</b> Attached ({ctx["doc_name"]})</div>'
    return HEADER + f"""
<tr><td style="padding:24px;font-family:Arial,Helvetica,sans-serif;color:#111827;">
  <p style="margin:0 0 12px;">Dear Admin,</p>
  <p style="margin:0 0 16px;">A new leave application has been submitted. Please review the details below and select an action.</p>

  <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;margin:0 0 16px;">
    <tr><td style="padding:12px 16px;">
      <div><b>Name:</b> {ctx["student_name"]}</div>
      <div><b>Email:</b> {ctx["student_email"]}</div>
      <div><b>Course:</b> {ctx.get("program","-")}</div>
      <div><b>Leave Dates:</b> {ctx["from_date"]} – {ctx["to_date"]}</div>
      <div><b>Reason:</b> {ctx["reason"]}</div>
      {doc_html}
      <div><b>Application ID:</b> {ctx["application_id"]}</div>
    </td></tr>
  </table>

  <p style="margin:0 0 12px;"><i>For security, you’ll confirm this action on the site.</i></p>

  <div style="margin:18px 0;">
    <a href="{ctx["base_url"]}/?aid={ctx["application_id"]}&action=approve&t={ctx["approve_token"]}"
       style="background:#059669;color:#ffffff;text-decoration:none;padding:12px 18px;border-radius:6px;display:inline-block;font-weight:bold;">
       Approve
    </a>
    <span style="display:inline-block;width:12px;"></span>
    <a href="{ctx["base_url"]}/?aid={ctx["application_id"]}&action=reject&t={ctx["reject_token"]}"
       style="background:#dc2626;color:#ffffff;text-decoration:none;padding:12px 18px;border-radius:6px;display:inline-block;font-weight:bold;">
       Reject
    </a>
  </div>
</td></tr>
""" + FOOTER

def tmpl_admin_confirm(ctx: dict) -> str:
    return HEADER + f"""
<tr><td style="padding:24px;font-family:Arial,Helvetica,sans-serif;color:#111827;">
  <p style="margin:0 0 12px;">Dear Admin,</p>
  <p style="margin:0 0 16px;">Your decision for the leave application below has been processed.</p>
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;margin:0 0 16px;">
    <tr><td style="padding:12px 16px;">
      <div><b>Status:</b> {ctx["status"]}</div>
      <div><b>Name:</b> {ctx["student_name"]}</div>
      <div><b>Dates:</b> {ctx["from_date"]} – {ctx["to_date"]}</div>
      <div><b>Application ID:</b> {ctx["application_id"]}</div>
      <div><b>Processed At:</b> {ctx["processed_at"]} (Asia/Kolkata)</div>
    </td></tr>
  </table>
  <p style="margin:0;">Regards,<br/>Leave Application System</p>
</td></tr>
""" + FOOTER

def tmpl_security_approved(ctx: dict) -> str:
    return HEADER + f"""
<tr><td style="padding:24px;font-family:Arial,Helvetica,sans-serif;color:#111827;">
  <p style="margin:0 0 12px;">Dear Security Team,</p>
  <p style="margin:0 0 12px;">Please note the approved leave below:</p>

  <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;margin:0 0 16px;">
    <tr><td style="padding:12px 16px;">
      <div><b>Student:</b> {ctx["student_name"]} ({ctx["student_email"]})</div>
      <div><b>Course:</b> {ctx.get("program","-")}</div>
      <div><b>Leave Window:</b> {ctx["from_date"]} – {ctx["to_date"]}</div>
      <div><b>Reason:</b> {ctx["reason"]}</div>
      <div><b>Parent Contact:</b> {ctx.get("parent_name","-")} • {ctx.get("parent_email","-")} • {ctx.get("parent_mobile","-")}</div>
      <div><b>Application ID:</b> {ctx["application_id"]}</div>
    </td></tr>
  </table>

  <p style="margin:0;">Please arrange access accordingly during this window.</p>
</td></tr>
""" + FOOTER

def tmpl_parent_approved(ctx: dict) -> str:
    return HEADER + f"""
<tr><td style="padding:24px;font-family:Arial,Helvetica,sans-serif;color:#111827;">
  <p style="margin:0 0 12px;">Dear {ctx.get("parent_name","Parent")},</p>
  <p style="margin:0 0 12px;">We’re writing to inform you that {ctx["student_name"]}’s leave request has been <b>approved</b>.</p>
  <ul style="margin:0 0 16px;padding-left:18px;">
    <li><b>Dates:</b> {ctx["from_date"]} – {ctx["to_date"]}</li>
    <li><b>Reason:</b> {ctx["reason"]}</li>
    <li><b>Application ID:</b> {ctx["application_id"]}</li>
  </ul>
  <p style="margin:0;">Regards,<br/>Woxsen University</p>
</td></tr>
""" + FOOTER

def tmpl_parent_rejected(ctx: dict) -> str:
    note = f'<p style="margin:0 0 12px;"><b>Note:</b> {ctx["rejection_note"]}</p>' if ctx.get("rejection_note") else ""
    return HEADER + f"""
<tr><td style="padding:24px;font-family:Arial,Helvetica,sans-serif;color:#111827;">
  <p style="margin:0 0 12px;">Dear {ctx.get("parent_name","Parent")},</p>
  <p style="margin:0 0 12px;">We’re writing to inform you that {ctx["student_name"]}’s leave request has been <b>rejected</b>.</p>
  <ul style="margin:0 0 16px;padding-left:18px;">
    <li><b>Dates:</b> {ctx["from_date"]} – {ctx["to_date"]}</li>
    <li><b>Reason:</b> {ctx["reason"]}</li>
    <li><b>Application ID:</b> {ctx["application_id"]}</li>
  </ul>
  {note}
  <p style="margin:0;">Regards,<br/>Woxsen University</p>
</td></tr>
""" + FOOTER

def tmpl_student_approved(ctx: dict) -> str:
    doc = f'<p style="margin:0 0 12px;">Document on file: <a href="{ctx["doc_url"]}">View</a></p>' if ctx.get("doc_url") else ""
    return HEADER + f"""
<tr><td style="padding:24px;font-family:Arial,Helvetica,sans-serif;color:#111827;">
  <p style="margin:0 0 12px;">Dear {ctx["student_name"]},</p>
  <p style="margin:0 0 12px;">Your leave request has been <b>approved</b>.</p>
  <ul style="margin:0 0 16px;padding-left:18px;">
    <li><b>Dates:</b> {ctx["from_date"]} – {ctx["to_date"]}</li>
    <li><b>Reason:</b> {ctx["reason"]}</li>
    <li><b>Application ID:</b> {ctx["application_id"]}</li>
  </ul>
  {doc}
  <p style="margin:0;">Regards,<br/>Woxsen University</p>
</td></tr>
""" + FOOTER

def tmpl_student_rejected(ctx: dict) -> str:
    note = f'<p style="margin:0 0 12px;"><b>Note:</b> {ctx["rejection_note"]}</p>' if ctx.get("rejection_note") else ""
    return HEADER + f"""
<tr><td style="padding:24px;font-family:Arial,Helvetica,sans-serif;color:#111827;">
  <p style="margin:0 0 12px;">Dear {ctx["student_name"]},</p>
  <p style="margin:0 0 12px;">Your leave request has been <b>rejected</b>.</p>
  <ul style="margin:0 0 16px;padding-left:18px;">
    <li><b>Dates:</b> {ctx["from_date"]} – {ctx["to_date"]}</li>
    <li><b>Reason:</b> {ctx["reason"]}</li>
    <li><b>Application ID:</b> {ctx["application_id"]}</li>
  </ul>
  {note}
  <p style="margin:0;">Regards,<br/>Woxsen University</p>
</td></tr>
""" + FOOTER

# ==============================
# Business logic
# ==============================

def send_admin_review_email(payload: dict, approve_token: str, reject_token: str):
    ctx = {
        "base_url": PUBLIC_BASE_URL,  # Use configured public base URL
        "application_id": payload["application_id"],
        "student_name": payload["student_name"],
        "student_email": payload["student_email"],
        "program": payload.get("program","-"),
        "from_date": payload["from_date"],
        "to_date": payload["to_date"],
        "reason": payload["reason"],
        "doc_url": payload.get("doc_url",""),
        "doc_name": payload.get("doc_name") or "",
        "doc_attached": bool(payload.get("doc_bytes")),
        "approve_token": approve_token,
        "reject_token": reject_token,
    }
    subject = f"New Leave Application – {ctx['student_name']} ({ctx['from_date']} to {ctx['to_date']})"
    html = tmpl_admin_review(ctx)
    attachments = []
    if payload.get("doc_bytes") and payload.get("doc_name"):
        attachments.append((payload["doc_name"], payload["doc_bytes"], payload.get("doc_mime") or "application/octet-stream"))
    ok, err, msgid = send_html(ADMIN_EMAIL, subject, html, "admin", payload["application_id"],
                               headers=None, attachments=attachments)
    # Persist the Message-ID so we can reply later
    if ok and msgid:
        with db() as con:
            con.execute("UPDATE leave_applications SET admin_review_msgid=? WHERE application_id=?",
                        (msgid, payload["application_id"]))

def send_decision_notifications(a_row: sqlite3.Row, status: str, rejection_note: str | None = None):
    processed_at = datetime.now(IST).strftime("%B %d, %Y %I:%M %p")
    ctx = {
        "status": status,
        "application_id": a_row["application_id"],
        "student_name": a_row["student_name"],
        "student_email": a_row["student_email"],
        "program": a_row["program"] or "-",
        "from_date": a_row["from_date"],
        "to_date": a_row["to_date"],
        "reason": a_row["reason"],
        "doc_url": "",  # if you host docs, populate here
        "processed_at": processed_at,
        "parent_name": a_row["father_name"] or a_row["mother_name"] or "Parent",
        "parent_email": a_row["father_email"] or a_row["mother_email"] or "",
        "parent_mobile": a_row["father_mobile"] or a_row["mother_mobile"] or "",
        "rejection_note": rejection_note or "",
    }

    # --- Admin confirmation as a REPLY to the original admin review email
    orig_msgid = a_row["admin_review_msgid"]
    reply_headers = {"In-Reply-To": orig_msgid, "References": orig_msgid} if orig_msgid else None
    # Keep the same "New Leave Application – ..." subject but with "Re:"
    orig_subject = f"New Leave Application – {ctx['student_name']} ({ctx['from_date']} to {ctx['to_date']})"
    reply_subject = f"Re: {orig_subject}"
    _ok, _err, _ = send_html(
        ADMIN_EMAIL,
        reply_subject,
        tmpl_admin_confirm(ctx),
        "admin_confirm",
        a_row["application_id"],
        headers=reply_headers
    )

    # --- Security / Parents / Student (not threaded)
    if status == "APPROVED":
        _ok, _err, _ = send_html(
            SECURITY_EMAIL,
            f"Approved Leave – {ctx['student_name']} ({ctx['from_date']} to {ctx['to_date']})",
            tmpl_security_approved(ctx),
            "security",
            a_row["application_id"]
        )
        if ctx["parent_email"]:
            _ok, _err, _ = send_html(
                ctx["parent_email"],
                f"Leave Approved – {ctx['student_name']} ({ctx['from_date']} to {ctx['to_date']})",
                tmpl_parent_approved(ctx),
                "parent",
                a_row["application_id"]
            )
        _ok, _err, _ = send_html(
            ctx["student_email"],
            f"Your Leave is Approved – {ctx['from_date']} to {ctx['to_date']}",
            tmpl_student_approved(ctx),
            "student",
            a_row["application_id"]
        )
    else:
        if ctx["parent_email"]:
            _ok, _err, _ = send_html(
                ctx["parent_email"],
                f"Leave Decision – {ctx['student_name']} ({ctx['from_date']} to {ctx['to_date']})",
                tmpl_parent_rejected(ctx),
                "parent",
                a_row["application_id"]
            )
        _ok, _err, _ = send_html(
            ctx["student_email"],
            "Your Leave Request – Decision",
            tmpl_student_rejected(ctx),
            "student",
            a_row["application_id"]
        )

def process_action(aid: str, token: str, action: str, rejection_note: str | None = None) -> str:
    now = datetime.now(IST)
    with db() as con:
        # NO manual BEGIN; rely on autocommit
        row = con.execute("""
            SELECT status, token_expires_at, approve_token_hash, reject_token_hash
            , admin_review_msgid
            FROM leave_applications WHERE application_id=?
        """, (aid,)).fetchone()
        if not row:
            st.error(f"Application not found for application_id: {aid}")
            return "Application not found."
        if row["status"] in ("APPROVED","REJECTED"):
            return "This leave application has already been processed."
        if now > datetime.fromisoformat(row["token_expires_at"]):
            return "This action has already been processed or the token has expired."

        expected = row["approve_token_hash"] if action == "approve" else row["reject_token_hash"]
        if _sha256(token) != expected:
            return "This action has already been processed or the token has expired."

        new_status = "APPROVED" if action == "approve" else "REJECTED"
        con.execute("UPDATE leave_applications SET status=?, decided_at=?, decided_by=? WHERE application_id=?",
                    (new_status, now.isoformat(), ADMIN_EMAIL, aid))
        a_row = con.execute("SELECT * FROM leave_applications WHERE application_id=?", (aid,)).fetchone()

    # Send notifications after commit
    send_decision_notifications(a_row, new_status, rejection_note=rejection_note if action == "reject" else None)
    return f"Leave application {new_status.lower()}."

# ==============================
# UI: Approval confirmation route
# ==============================

def approval_route():
    # Streamlit changed API for query params over time; try both
    try:
        params = st.query_params  # modern
    except Exception:
        params = st.experimental_get_query_params()  # legacy

    def _first(v):
        if v is None:
            return None
        if isinstance(v, list):
            return v[0] if v else None
        return v

    aid = _first(params.get("aid"))
    action = _first(params.get("action"))
    token = _first(params.get("t"))

    if not (aid and action in ("approve","reject") and token):
        return False  # not on approval route

    st.header(f"{action.title()} Leave Application")
    st.info("For security, please confirm this action.")
    rejection_note = ""
    if action == "reject":
        rejection_note = st.text_area("Optional note to include in the rejection emails (not required):", "")

    if st.button(f"Confirm {action.title()}"):
        msg = process_action(aid, token, action, rejection_note=rejection_note)
        st.success(msg)
        st.stop()
    st.stop()
    return True

# ==============================
# UI: Student submission form
# ==============================

def date_to_text(d: date) -> str:
    return d.strftime("%B %d, %Y")

def generate_numeric_id_from_uuid():
    new_uuid = uuid.uuid4()
    hashed_uuid = hashlib.sha256(new_uuid.bytes).hexdigest()
    numeric_id = int(hashed_uuid[:16], 16)
    return str(numeric_id)

def submission_form():
    st.title("Out-Gate Leave Application")

    df = load_students_csv(STUDENTS_CSV_PATH)

    st.subheader("Student Details")
    student_email_input = st.text_input("Student Email", key="student_email_input", placeholder="john.doe@student.woxsen.edu.in")

    student_row = None
    student_email_on_file = ''
    if student_email_input and not df.empty:
        # case-insensitive match on email column(s)
        email_cols = [c for c in df.columns if "email" in c.lower()]
        if email_cols:
            mask = False
            for c in email_cols:
                mask = mask | (df[c].astype(str).str.lower() == student_email_input.strip().lower())
            matches = df[mask]
            if not matches.empty:
                student_row = matches.iloc[0]

    if student_row is None:
        st.caption("Enter your university email to auto-fill your details from master data.")
    else:
        # Extract fields with flexible names
        student_email_on_file = ci_get(student_row, ["Candidate Adress Email"], student_email_input).strip()
        student_name = get_student_name(student_row, fallback_email=student_email_on_file or student_email_input)
        program = ci_get(student_row, [ "Course","Programs", "Program"], "")
        semester = ci_get(student_row, ["Semester"], "")
        section = ci_get(student_row, ["Section"], "")
        father_name = ci_get(student_row, ["Father Name", "Father's Name"], "")
        father_mobile = ci_get(student_row, ["Father Mobile Number", "Father Mobile No.", "Father Mobile", "Father Phone"], "")
        father_email = ci_get(student_row, ["Father Adress Email", "Father Email", "Parent Email"], "")
        mother_name = ci_get(student_row, ["Mother Name", "Mother's Name"], "")
        mother_email = ci_get(student_row, ["Mother Address Email"], "")
        mother_mobile = ci_get(student_row, ["Mother Mobile Number", "Mother Mobile No.", "Mother Mobile", "Mother Phone", "Guardian 2 Mobile No"], "")

        st.write(f"**Name:** {student_name or '-'}")
        st.write(f"**Course:** {program or '-'}")
        display_email = student_email_on_file or student_email_input
        if display_email:
            st.write(f"**Email on file:** {display_email}")
        parent_bits = []
        if father_name or father_email or father_mobile:
            parts = [father_name or '-']
            if father_email:
                parts.append(mask_email(father_email))
            if father_mobile:
                parts.append(mask_phone(father_mobile))
            parent_bits.append(" | ".join(parts))
        if mother_name or mother_email or mother_mobile:
            parts = [mother_name or '-']
            if mother_email:
                parts.append(mask_email(mother_email))
            if mother_mobile:
                parts.append(mask_phone(mother_mobile))
            parent_bits.append(" | ".join(parts))
        if parent_bits:
            st.write(f"**Parent on file:** {'; '.join(parent_bits)}")
        if DEV_MODE and not student_name:
            st.caption("(dev) Name columns not found; falling back. Available columns: " + ", ".join(student_row.index))

    st.subheader("Leave Details")
    col1, col2 = st.columns(2)
    with col1:
        from_dt = st.date_input("From (inclusive)", min_value=date.today())
    with col2:
        to_dt = st.date_input("To (inclusive)", min_value=from_dt if 'from_dt' in locals() else date.today())

    reason = st.text_area("Reason", help="Be concise; 1–2 lines are sufficient.", max_chars=MAX_REASON_LEN)
    upload = st.file_uploader("Optional Supporting Document (PDF/PNG/JPG)", type=[e.strip(".") for e in ALLOWED_DOC_EXTS])

    if st.button("Submit Application"):
        if not student_email_input:
            st.error("Student Email is required."); return
        if student_row is None:
            st.error("Email not found in master data. Please check and try again."); return
        if from_dt > to_dt:
            st.error("From date must be on or before To date."); return
        if from_dt < date.today():
            st.error("Leave must start today or in the future."); return
        duration = (to_dt - from_dt).days + 1
        if duration > MAX_LEAVE_DAYS:
            st.error(f"Leave duration cannot exceed {MAX_LEAVE_DAYS} days (requested {duration})."); return
        if not reason or not reason.strip():
            st.error("Reason is required."); return

        reason_type = "MEDICAL" if "medical" in reason.lower() else "OTHER"
        if reason_type == "MEDICAL" and upload is None:
            st.error("Medical leave requires a supporting document (PDF/PNG/JPG)."); return
        if upload is not None and not ext_ok(upload.name):
            st.error("Unsupported file type. Please upload PDF/PNG/JPG."); return

        application_id = generate_numeric_id_from_uuid()
        # ---- initialize doc vars so they exist even when no upload ----
        doc_name = None
        doc_sha256 = None
        doc_mime = None
        doc_bytes_for_mail = None
        if upload is not None:
            data = upload.read() or b""
            doc_sha256 = hashlib.sha256(data).hexdigest()
            doc_name = upload.name
            doc_mime = upload.type or (mimetypes.guess_type(upload.name)[0] or "application/octet-stream")
            # Respect size cap for emailing
            if len(data) > MAX_ATTACHMENT_BYTES:
                st.warning(f"Attachment too large to email ({len(data)//1024} KB > {MAX_ATTACHMENT_BYTES//1024} KB). It will not be attached.")
                doc_bytes_for_mail = None
            else:
                doc_bytes_for_mail = data

        student_email_final = (student_email_on_file or student_email_input).strip()
        payload = {
            "application_id": application_id,
            "from_date": date_to_text(from_dt),
            "to_date": date_to_text(to_dt),
            "reason": reason.strip(),
            "reason_type": reason_type,
            "doc_name": doc_name,
            "doc_sha256": doc_sha256,
            "doc_url": "",
            "doc_bytes": doc_bytes_for_mail,         # may be None if too large or no file
            "doc_mime": doc_mime if upload is not None else None,
            "student_email": student_email_final,
            "student_name": get_student_name(student_row, fallback_email=student_email_final),
            "program": ci_get(student_row, ["Course","Programs", "Program"], ""),
            "semester": ci_get(student_row, ["Semester"], ""),
            "section": ci_get(student_row, ["Section"], ""),
            "father_name": ci_get(student_row, ["Father Name", "Father's Name"], ""),
            "father_mobile": ci_get(student_row, ["Father Mobile Number", "Father Mobile No.", "Father Mobile", "Father Phone"], ""),
            "father_email": ci_get(student_row, ["Father Adress Email", "Father Email", "Parent Email"], ""),
            "mother_name": ci_get(student_row, ["Mother Name", "Mother's Name"], ""),
            "mother_email": ci_get(student_row, ["Mother Address Email"], ""),
            "mother_mobile": ci_get(student_row, ["Mother Mobile Number", "Mother Mobile No.", "Mother Mobile", "Mother Phone"], ""),
        }

        approve_token = secrets.token_urlsafe(32)
        reject_token = secrets.token_urlsafe(32)
        exp = datetime.now(IST) + timedelta(hours=TOKEN_TTL_HOURS)

        try:
            insert_application(payload, _sha256(approve_token), _sha256(reject_token), exp.isoformat())
        except Exception as e:
            st.error(f"Failed to persist your application. Please try again. ({e})"); return

        send_admin_review_email(payload, approve_token, reject_token)

        st.success("✅ Application submitted. Admin has been notified via email.")
        st.info(f"Your Application ID: **{application_id}**")

        if DEV_MODE:
            row = get_application(application_id)
            if row:
                st.caption(f"(dev) Insert verified for application_id={row['application_id']}")

# ==============================
# Main
# ==============================

def main():
    st.set_page_config(page_title="Woxsen Out-Gate Leave", page_icon="📝", layout="centered")

    if approval_route():
        return

    submission_form()

if __name__ == "__main__":
    main()
