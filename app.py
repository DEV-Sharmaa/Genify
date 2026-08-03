from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
from datetime import timedelta, datetime
import requests
import psycopg2
import psycopg2.extras
import base64
import os
import re
import io
import gc
import json
from fpdf import FPDF

# ── CONFIG ───────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    print("⚠️  WARNING: GEMINI_API_KEY not set. Paper generation will fail.")

# Using the REST API directly instead of the google-generativeai SDK.
# The SDK pulls in grpcio, which is heavy (channel + thread pool overhead)
# and is the main reason the app was getting SIGKILL'd on Render's 512MB
# free tier. Plain HTTPS requests avoid that entirely.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_TIMEOUT_SECS = int(os.environ.get("GEMINI_TIMEOUT_SECS", "120"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-production-immediately")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_SECURE"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Reject oversized uploads at the Flask/Werkzeug layer, before the file
# is ever read into memory. This is the biggest lever for capping peak
# RAM per request on a 512MB instance.
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "15"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# Postgres (Supabase free tier) — replaces the old local SQLite file, which
# got wiped every time Render's free-tier instance restarted or redeployed
# since that disk is ephemeral. Set DATABASE_URL on Render to the
# "Transaction pooler" connection string from Supabase → Connect.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    print("⚠️  WARNING: DATABASE_URL not set. All database operations will fail.")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "genify2024")

# ── FIREBASE CLOUD STORAGE ───────────────────────
# Set these two env vars on Render to turn on cloud saving of every
# generated PDF. Left unset, the app just skips cloud save silently —
# your own download always works either way.
#   FIREBASE_STORAGE_BUCKET     e.g. genify-a3890.firebasestorage.app
#   FIREBASE_SERVICE_ACCOUNT_JSON   the full contents of a Firebase
#     service-account JSON key, pasted as a single-line env var value
#     (Firebase console → Project settings → Service accounts →
#     Generate new private key). Kept as an env var, not a file, since
#     Render's free-tier disk is ephemeral and wiped on every deploy.
FIREBASE_STORAGE_BUCKET = os.environ.get("FIREBASE_STORAGE_BUCKET", "")
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")

FREE_PAPER_LIMIT = 5
STARTER_MONTHLY_LIMIT = 30

# Seed list — also stored in DB after first login
PRO_USERS = [
    "devaidomain@gmail.com",
]


# ── DATABASE ─────────────────────────────────────
class _DBConn:
    """
    Thin wrapper around a real psycopg2 connection. psycopg2's connection
    object is a C-extension type, so you can't attach a new attribute to
    an instance of it directly (unlike sqlite3.Connection, which is a
    plain Python object and allowed conn.execute = ... just fine). This
    wrapper is an ordinary Python class instead, so it supports adding
    the execute(sql, params) convenience method every call site in this
    file already expects, without touching those call sites.
    """
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(sql.replace("?", "%s"), params)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_db():
    """
    Returns a _DBConn wrapping a psycopg2 connection, exposing the same
    conn.execute(sql, params).fetchone()/.fetchall() style every call
    site in this file was already written against for sqlite3. Also
    auto-converts sqlite's "?" placeholders to psycopg2's "%s", so query
    strings didn't need rewriting either. RealDictCursor makes each row
    behave like a dict already, matching the old sqlite3.Row + dict(row)
    pattern used throughout this file.
    """
    conn = psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    return _DBConn(conn)


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            photo TEXT DEFAULT '',
            plan TEXT DEFAULT 'Free',
            papers INTEGER DEFAULT 0,
            papers_month INTEGER DEFAULT 0,
            papers_month_key TEXT DEFAULT '',
            joined TEXT DEFAULT '',
            institute_name TEXT DEFAULT '',
            default_subject TEXT DEFAULT 'Mathematics',
            default_class TEXT DEFAULT 'Class 6',
            default_language TEXT DEFAULT 'English'
        )
    """)
    # Metadata for every PDF that gets uploaded to Firebase Storage —
    # lets you list/re-download a teacher's past papers later without
    # storing the PDFs themselves anywhere on Render's ephemeral disk.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS papers (
            id SERIAL PRIMARY KEY,
            user_email TEXT NOT NULL,
            subject TEXT,
            class TEXT,
            marks TEXT,
            storage_path TEXT,
            download_url TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


# ── FIREBASE CLOUD STORAGE HELPERS ──────────────
_storage_bucket = None  # lazily built, cached after first successful init


def get_firebase_bucket():
    """
    Lazily builds a google-cloud-storage bucket handle from a service
    account JSON stored in an env var — no credentials file needed on
    disk, so this works cleanly on Render's ephemeral filesystem.

    Deliberately uses google-cloud-storage directly instead of the
    firebase-admin SDK: firebase-admin pulls in grpcio transitively
    (via its Firestore module) even when you only touch Storage, which
    would reintroduce the exact memory problem the Gemini REST switch
    was meant to fix. google-cloud-storage is plain REST/HTTPS.

    Returns None (never raises) if not configured or if init fails —
    callers must treat cloud save as optional, not required.
    """
    global _storage_bucket
    if _storage_bucket is not None:
        return _storage_bucket
    if not FIREBASE_STORAGE_BUCKET or not FIREBASE_SERVICE_ACCOUNT_JSON:
        return None
    try:
        from google.cloud import storage
        from google.oauth2 import service_account

        info = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(info)
        client = storage.Client(credentials=creds, project=info.get("project_id"))
        _storage_bucket = client.bucket(FIREBASE_STORAGE_BUCKET)
        return _storage_bucket
    except Exception as e:
        print(f"⚠️  Firebase Storage unavailable, skipping cloud save: {e}")
        return None


def upload_pdf_to_firebase(pdf_bytes, dest_path):
    """
    Uploads PDF bytes to Firebase Storage and returns a v4 signed URL
    valid for 7 days. Returns None on any failure — this must never
    raise, since cloud save is a bonus feature, not a requirement for
    the user getting their own PDF.
    """
    bucket = get_firebase_bucket()
    if bucket is None:
        return None
    try:
        blob = bucket.blob(dest_path)
        blob.upload_from_string(pdf_bytes, content_type="application/pdf")
        return blob.generate_signed_url(version="v4", expiration=timedelta(days=7))
    except Exception as e:
        print(f"⚠️  Firebase upload failed, continuing without it: {e}")
        return None


def save_paper_record(email, subject, cls, marks, storage_path, download_url):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO papers (user_email, subject, class, marks, storage_path, download_url, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (normalize_email(email), subject, cls, marks, storage_path, download_url, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_user_papers(email):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM papers WHERE user_email = ? ORDER BY id DESC",
        (normalize_email(email),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def refresh_signed_url(storage_path):
    """
    Re-signs a storage path on the fly. Signing is a local operation done
    with the service account's private key — no network round-trip — so
    it's cheap to call once per saved paper on every page load, and it
    means links never go stale even though each individual signed URL
    expires after 7 days (only the stored storage_path is permanent).
    Returns None if Firebase isn't configured or the path can't be signed.
    """
    bucket = get_firebase_bucket()
    if bucket is None or not storage_path:
        return None
    try:
        blob = bucket.blob(storage_path)
        return blob.generate_signed_url(version="v4", expiration=timedelta(days=7))
    except Exception as e:
        print(f"⚠️  Could not refresh signed URL for {storage_path}: {e}")
        return None


def current_month_key():
    return datetime.now().strftime("%Y-%m")


def normalize_email(email):
    return (email or "").strip().lower()


def seed_plan_from_list(email):
    if normalize_email(email) in [e.lower() for e in PRO_USERS]:
        return "Pro"
    return "Free"


def get_user_by_email(email):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (normalize_email(email),)).fetchone()
    conn.close()
    return dict(row) if row else None


def reset_starter_month_if_needed(user):
    if user["plan"] != "Starter":
        return user
    month = current_month_key()
    if user.get("papers_month_key") != month:
        conn = get_db()
        conn.execute(
            "UPDATE users SET papers_month = 0, papers_month_key = ? WHERE email = ?",
            (month, user["email"]),
        )
        conn.commit()
        conn.close()
        user["papers_month"] = 0
        user["papers_month_key"] = month
    return user


def get_or_create_user(email, name, photo=""):
    email = normalize_email(email)
    existing = get_user_by_email(email)
    conn = get_db()

    if existing:
        conn.execute(
            "UPDATE users SET name = ?, photo = ? WHERE email = ?",
            (name, photo, email),
        )
        conn.commit()
        conn.close()
        user = get_user_by_email(email)
    else:
        joined = datetime.now().strftime("%B %Y")
        plan = seed_plan_from_list(email)
        conn.execute(
            """
            INSERT INTO users (
                email, name, photo, plan, joined, papers_month_key
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (email, name, photo, plan, joined, current_month_key()),
        )
        conn.commit()
        conn.close()
        user = get_user_by_email(email)

    # Keep PRO_USERS list in sync with DB
    if seed_plan_from_list(email) == "Pro" and user["plan"] != "Pro":
        set_user_plan(email, "Pro")
        user = get_user_by_email(email)

    return reset_starter_month_if_needed(user)


def set_user_plan(email, plan):
    conn = get_db()
    conn.execute("UPDATE users SET plan = ? WHERE email = ?", (normalize_email(email), plan))
    conn.commit()
    conn.close()


def row_to_session(user):
    user = reset_starter_month_if_needed(user)
    is_pro = user["plan"] == "Pro"
    is_starter = user["plan"] == "Starter"

    if is_pro:
        papers_left = "unlimited"
    elif is_starter:
        papers_left = max(0, STARTER_MONTHLY_LIMIT - user["papers_month"])
    else:
        papers_left = max(0, FREE_PAPER_LIMIT - user["papers"])

    return {
        "name": user["name"],
        "email": user["email"],
        "photo": user["photo"],
        "plan": user["plan"],
        "is_pro": is_pro,
        "is_starter": is_starter,
        "papers": user["papers"],
        "papers_month": user["papers_month"],
        "papers_left": papers_left,
        "joined": user["joined"],
        "institute_name": user["institute_name"],
        "default_subject": user["default_subject"],
        "default_class": user["default_class"],
        "default_language": user["default_language"],
    }


def refresh_session_user():
    if "user" not in session:
        return None
    db_user = get_user_by_email(session["user"]["email"])
    if not db_user:
        session.clear()
        return None
    session["user"] = row_to_session(db_user)
    session.modified = True
    return session["user"]


def can_generate(user):
    user = reset_starter_month_if_needed(user)
    if user["plan"] == "Pro":
        return True, None
    if user["plan"] == "Starter":
        if user["papers_month"] >= STARTER_MONTHLY_LIMIT:
            return False, "starter_limit_reached"
        return True, None
    if user["papers"] >= FREE_PAPER_LIMIT:
        return False, "free_limit_reached"
    return True, None


def increment_usage(email):
    user = get_user_by_email(email)
    user = reset_starter_month_if_needed(user)
    conn = get_db()

    if user["plan"] == "Pro":
        conn.execute("UPDATE users SET papers = papers + 1 WHERE email = ?", (email,))
    elif user["plan"] == "Starter":
        conn.execute(
            "UPDATE users SET papers = papers + 1, papers_month = papers_month + 1 WHERE email = ?",
            (email,),
        )
    else:
        conn.execute("UPDATE users SET papers = papers + 1 WHERE email = ?", (email,))

    conn.commit()
    conn.close()


def save_user_settings(email, data):
    conn = get_db()
    conn.execute(
        """
        UPDATE users SET
            institute_name = ?,
            default_subject = ?,
            default_class = ?,
            default_language = ?
        WHERE email = ?
        """,
        (
            data.get("institute_name", ""),
            data.get("default_subject", "Mathematics"),
            data.get("default_class", "Class 6"),
            data.get("default_language", "English"),
            normalize_email(email),
        ),
    )
    conn.commit()
    conn.close()


try:
    init_db()
except Exception as e:
    print(f"⚠️  Database init failed — check DATABASE_URL on Render: {e}")


@app.errorhandler(413)
def too_large(e):
    return jsonify({
        "success": False,
        "error": f"File too large. Max upload size is {MAX_UPLOAD_MB}MB."
    }), 413


# ── GEMINI (REST, NOT SDK) ──────────────────────
class GeminiError(Exception):
    """Raised for any failure talking to Gemini, with a user-safe message."""
    pass


def call_gemini(parts, model_name=None, timeout=None):
    """
    Calls the Gemini REST API directly (no grpc/google-generativeai SDK).
    `parts` should be a list of REST-style part dicts, e.g.:
        [{"inline_data": {"mime_type": "...", "data": "<base64>"}}, {"text": "..."}]
    Returns the concatenated text of the response.
    Raises GeminiError with a short, user-facing message on any failure.
    """
    if not GEMINI_API_KEY:
        raise GeminiError("Server is missing its Gemini API key. Contact the site admin.")

    model_name = model_name or GEMINI_MODEL
    timeout = timeout or GEMINI_TIMEOUT_SECS
    url = f"{GEMINI_API_BASE}/models/{model_name}:generateContent"

    payload = {
        "contents": [
            {"role": "user", "parts": parts}
        ],
        "generationConfig": {
            "temperature": 0.7,
        },
    }

    try:
        resp = requests.post(
            url,
            params={"key": GEMINI_API_KEY},
            json=payload,
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        raise GeminiError("Gemini took too long to respond. Try again, or use a shorter PDF.")
    except requests.exceptions.RequestException as e:
        raise GeminiError(f"Could not reach Gemini: {e}")

    if resp.status_code == 429:
        raise GeminiError("Gemini rate limit hit. Please wait a moment and try again.")
    if resp.status_code >= 400:
        # Try to surface Gemini's own error message if present
        try:
            err_json = resp.json()
            msg = err_json.get("error", {}).get("message", resp.text[:300])
        except ValueError:
            msg = resp.text[:300]
        raise GeminiError(f"Gemini API error ({resp.status_code}): {msg}")

    try:
        data = resp.json()
    except ValueError:
        raise GeminiError("Gemini returned an unreadable response.")

    candidates = data.get("candidates") or []
    if not candidates:
        # Could be blocked by safety filters, etc.
        feedback = data.get("promptFeedback", {})
        block_reason = feedback.get("blockReason")
        if block_reason:
            raise GeminiError(f"Gemini blocked this request ({block_reason}). Try a different PDF.")
        raise GeminiError("Gemini returned no output. Try again.")

    text_chunks = []
    for cand in candidates:
        content = cand.get("content", {}) or {}
        for part in content.get("parts", []) or []:
            if "text" in part:
                text_chunks.append(part["text"])

    full_text = "\n".join(text_chunks).strip()
    if not full_text:
        raise GeminiError("Gemini returned an empty paper. Try again.")

    return clean_latex_artifacts(full_text)


# Common LaTeX wrappers/commands Gemini sometimes emits despite the prompt
# instruction. This is a best-effort fallback, not a full LaTeX parser —
# it just strips the raw markup so it doesn't show up literally in the PDF.
_LATEX_DELIMS = [
    (r"\$\$(.+?)\$\$", r"\1"),
    (r"\\\[(.+?)\\\]", r"\1"),
    (r"\\\((.+?)\\\)", r"\1"),
    (r"\$(.+?)\$", r"\1"),
]
_LATEX_REPLACEMENTS = [
    (r"\\times", "×"),
    (r"\\div", "÷"),
    (r"\\cdot", "·"),
    (r"\\pi", "π"),
    (r"\\sqrt\{([^{}]*)\}", r"√\1"),
    (r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"\1/\2"),
    (r"\\leq", "≤"),
    (r"\\geq", "≥"),
    (r"\\neq", "≠"),
    (r"\\left", ""),
    (r"\\right", ""),
]


def clean_latex_artifacts(text):
    for pattern, repl in _LATEX_DELIMS:
        text = re.sub(pattern, repl, text, flags=re.DOTALL)
    for pattern, repl in _LATEX_REPLACEMENTS:
        text = re.sub(pattern, repl, text)
    # Simple ^{2} / _{2} superscript/subscript braces left over
    text = re.sub(r"\^\{?(\w+)\}?", r"^\1", text)
    text = re.sub(r"_\{?(\w+)\}?", r"_\1", text)
    return text


def compute_section_counts(total_marks):
    """
    Works out how many 1-mark MCQs, 2-mark short-answer, and 5-mark
    long-answer questions to ask for, so the paper's marks always sum
    exactly to what the teacher picked on the slider (20-100).
    Previously the template was hard-coded to 13 questions / 30 marks
    no matter what was selected — this fixes that.
    """
    total_marks = int(total_marks)
    mcq_marks = round(total_marks * 0.20)
    short_marks = round(total_marks * 0.30)
    mcq_count = max(3, round(mcq_marks / 1))
    short_count = max(2, round(short_marks / 2))
    remaining = total_marks - mcq_count * 1 - short_count * 2
    long_count = max(1, round(remaining / 5))

    current_total = mcq_count * 1 + short_count * 2 + long_count * 5
    diff = total_marks - current_total
    mcq_count += diff
    if mcq_count < 1:
        # Only possible at very small totals — pull the shortfall from
        # the long-answer section instead so nothing goes negative.
        long_count = max(1, long_count + (mcq_count - 1))
        mcq_count = 1
        current_total = mcq_count * 1 + short_count * 2 + long_count * 5
        mcq_count += total_marks - current_total

    return mcq_count, short_count, long_count


MAX_EXTRACTED_CHARS = 40000  # ~10k tokens — plenty for one chapter, caps prompt/memory size


def extract_pdf_text(pdf_bytes):
    """
    Pulls plain text out of the uploaded PDF using pypdf (pure Python,
    no native/C dependencies, no extra memory risk on Render's tier).
    Returns '' if the PDF has no extractable text layer (e.g. a pure
    scan) — caller falls back to sending the raw PDF to Gemini in that
    case, since Gemini's document vision can still often read scans.
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        chunks = []
        total_len = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            if text:
                chunks.append(text)
                total_len += len(text)
            if total_len >= MAX_EXTRACTED_CHARS:
                break
        full_text = "\n".join(chunks).strip()
        return full_text[:MAX_EXTRACTED_CHARS]
    except Exception as e:
        print(f"⚠️  PDF text extraction failed, will fall back to raw PDF: {e}")
        return ""


# ── PDF GENERATION HELPER ───────────────────────
def generate_question_paper_pdf(paper_text, subject="Subject", cls="Class", marks="40"):
    """
    Converts the generated question paper text into a clean, formatted PDF.
    """
    pdf = FPDF()
    pdf.add_font("DejaVu", "", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", uni=True)
    pdf.add_font("DejaVu", "B", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", uni=True)

    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    lines = paper_text.split("\n")
    pdf.set_font("DejaVu", "B", 16)

    for line in lines:
        stripped = line.strip()

        # Skip empty lines but add spacing
        if not stripped:
            pdf.ln(4)
            continue

        # Full-width separator lines (═══) — draw a horizontal line
        if "══" in stripped:
            pdf.ln(3)
            pdf.set_draw_color(0, 0, 0)
            pdf.set_line_width(0.5)
            pdf.line(10, pdf.get_y(), 200, pdf.get_y())
            pdf.ln(5)
            continue

        # Section headers (SECTION A, SECTION B, etc.)
        if stripped.startswith("SECTION"):
            pdf.ln(4)
            pdf.set_font("DejaVu", "B", 12)
            pdf.set_text_color(0, 51, 102)
            pdf.cell(0, 8, stripped, ln=True)
            pdf.set_text_color(0, 0, 0)
            pdf.ln(2)
            continue

        # Question lines (Q1., Q2., etc.)
        if re.match(r"^Q\d+[\.\)]", stripped):
            pdf.set_font("DejaVu", "", 10)
            pdf.multi_cell(0, 6, stripped)
            pdf.ln(1)
            continue

        # Option lines ((a), (b), etc.)
        if re.match(r"^\s*\([a-dA-D]\)", stripped):
            pdf.set_font("DejaVu", "", 9)
            pdf.multi_cell(0, 5, stripped)
            pdf.ln(0.5)
            continue

        # General Instructions
        if "General Instructions" in stripped:
            pdf.ln(3)
            pdf.set_font("DejaVu", "B", 11)
            pdf.cell(0, 7, stripped, ln=True)
            pdf.set_font("DejaVu", "", 9)
            pdf.ln(1)
            continue

        # Numbered instructions (1., 2., 3.)
        if re.match(r"^\d+\.", stripped):
            pdf.set_font("DejaVu", "", 9)
            pdf.multi_cell(0, 5, stripped)
            pdf.ln(1)
            continue

        # Header/title lines (centered, bold)
        if "═" in stripped or stripped.isupper() or stripped.startswith("GENIFY") or stripped.startswith("ANSWER KEY"):
            pdf.set_font("DejaVu", "B", 12)
            pdf.cell(0, 8, stripped, ln=True, align="C")
            pdf.ln(2)
            continue

        # Footer/end markers
        if "***" in stripped or "END OF" in stripped.upper():
            pdf.ln(3)
            pdf.set_font("DejaVu", "B", 12)
            pdf.cell(0, 8, stripped, ln=True, align="C")
            pdf.ln(2)
            continue

        # Default: regular text
        pdf.set_font("DejaVu", "", 10)
        pdf.multi_cell(0, 5, stripped)
        pdf.ln(1)

    # Add footer with Genify branding
    pdf.ln(5)
    pdf.set_font("DejaVu", "", 8)
    pdf.set_text_color(128, 128, 128)
    pdf.cell(0, 5, "Generated by Genify — genify.ai", ln=True, align="C")

    pdf.set_text_color(0, 0, 0)

    # Generate PDF bytes
    pdf_bytes = pdf.output()
    return io.BytesIO(pdf_bytes)


# ── ROUTES ───────────────────────────────────────
@app.route("/")
def landing():
    if "user" in session:
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.route("/login")
def login():
    if "user" in session:
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/login/google", methods=["POST"])
def google_login():
    data = request.json or {}
    email = data.get("email", "").strip()
    name = data.get("name", "Teacher").strip()
    photo = data.get("photo", "")
    uid = data.get("uid", "")  # Firebase UID — accepted but not persisted yet (see note below)

    if not email:
        return jsonify({"success": False, "error": "Email required"})

    db_user = get_or_create_user(email, name, photo)
    session.permanent = True
    session["user"] = row_to_session(db_user)
    session["user"]["uid"] = uid  # available this request; dropped on next refresh_session_user()
    session.modified = True
    return jsonify({"success": True})


@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect(url_for("login"))
    user = refresh_session_user()
    if not user:
        return redirect(url_for("login"))
    return render_template("dashboard.html", user=user)


@app.route("/profile")
def profile():
    if "user" not in session:
        return redirect(url_for("login"))
    user = refresh_session_user()
    if not user:
        return redirect(url_for("login"))
    return render_template("profile.html", user=user)


@app.route("/pricing")
def pricing():
    user = session.get("user")
    if user:
        user = refresh_session_user()  # may return None if the session is stale — that's fine here, pricing.html already handles a falsy user
    return render_template("pricing.html", user=user)


@app.route("/my-papers")
def my_papers():
    if "user" not in session:
        return redirect(url_for("login"))
    user = refresh_session_user()
    if not user:
        return redirect(url_for("login"))

    firebase_configured = get_firebase_bucket() is not None
    papers = []
    if firebase_configured:
        raw_papers = get_user_papers(user["email"])
        for p in raw_papers:
            p["fresh_url"] = refresh_signed_url(p["storage_path"]) or p["download_url"]
            papers.append(p)

    return render_template(
        "my_papers.html",
        user=user,
        papers=papers,
        firebase_configured=firebase_configured,
    )


@app.route("/settings")
def settings():
    if "user" not in session:
        return redirect(url_for("login"))
    user = refresh_session_user()
    if not user:
        return redirect(url_for("login"))
    return render_template("settings.html", user=user)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))


@app.route("/generate", methods=["POST"])
def generate():
    if "user" not in session:
        return jsonify({"success": False, "error": "Not logged in"})

    user = refresh_session_user()
    if not user:
        return jsonify({"success": False, "error": "Your session has expired. Please log in again."})
    db_user = get_user_by_email(user["email"])
    ok, err = can_generate(db_user)
    if not ok:
        return jsonify({"success": False, "error": err})

    try:
        pdf_file = request.files.get("pdf")
        if not pdf_file or pdf_file.filename == "":
            return jsonify({"success": False, "error": "Please upload a PDF file."})

        subject = request.form.get("subject", db_user["default_subject"])
        cls = request.form.get("cls", db_user["default_class"])
        difficulty = request.form.get("difficulty", "medium")
        marks = request.form.get("marks", "40")
        language = request.form.get("language", db_user["default_language"])
        time_allow = round(int(marks) * 1.5)
        out_lang = "Hindi (Devanagari script)" if language == "Hindi" else "English"
        user_is_pro = db_user["plan"] == "Pro"

        if language == "Hindi" and not user_is_pro:
            return jsonify({"success": False, "error": "hindi_pro_only"})

        institute = (db_user.get("institute_name") or "").strip()
        header_line = institute if (user_is_pro and institute) else "GENIFY — QUESTION PAPER"

        # Scale question counts so the paper's marks always sum to exactly
        # what was picked on the slider, instead of a fixed 13-question
        # skeleton regardless of the target.
        mcq_count, short_count, long_count = compute_section_counts(marks)

        def numbered_lines(start, count, mark_value, with_options):
            lines = []
            for i in range(count):
                line = f"Q{start + i}. [question] [{mark_value}]"
                lines.append(line)
                if with_options:
                    lines.append("    (a) option  (b) option  (c) option  (d) option")
            return "\n".join(lines)

        section_a = numbered_lines(1, mcq_count, 1, with_options=True)
        section_b = numbered_lines(mcq_count + 1, short_count, 2, with_options=False)
        section_c = numbered_lines(mcq_count + short_count + 1, long_count, 5, with_options=False)

        prompt = f"""You are a senior CBSE/ICSE examiner with 15+ years of experience setting
official board-standard question papers. Generate a complete, exam-ready
question paper in {out_lang} based strictly on the uploaded chapter content.

Subject: {subject} | Class: {cls} | Difficulty: {difficulty} | Total Marks: {marks}

QUALITY BAR — this paper represents a paying institute's brand to its
students, so it must read as genuinely professional, not generic or
filler:
- Every question must be directly answerable from the uploaded chapter —
  never invent facts, numbers, or topics absent from the source material.
- No two questions may test the same fact or concept — each question
  must probe something distinct from the chapter.
- MCQ distractors (wrong options) must be plausible, not obviously silly
  or unrelated — they should reflect common student misconceptions.
- Match the stated difficulty honestly: "easy" means direct recall,
  "medium" means applying a concept, "hard" means multi-step reasoning
  or analysis — do not label recall questions as "hard" or vice versa.
- Vary question phrasing naturally (definitions, fill-in-the-blank style,
  scenario-based, "which of the following", etc.) rather than repeating
  the same sentence structure for every question in a section.

CRITICAL FORMATTING RULE: This paper will be rendered as plain text, NOT LaTeX
or Markdown. Never use LaTeX commands or math-mode syntax anywhere — no
\\frac, \\sqrt, \\times, \\cdot, \\pi, \\left, \\right, ^{{}}, _{{}}, and no
$ or \\( \\) \\[ \\] delimiters. Write every mathematical expression using
plain Unicode characters instead, for example: × (multiply), ÷ (divide),
√ (root), π (pi), ² ³ (superscripts), ½ ¾ (fractions), ≤ ≥ ≠ (comparisons).
Example — write "a² + b² = c²", NOT "$a^2 + b^2 = c^2$" or "\\(a^{{2}}\\)".

LANGUAGE RULE: Even when writing the paper in Hindi, keep every structural
marker in this template EXACTLY as shown in English — "SECTION A",
"SECTION B", "SECTION C", "General Instructions", "Q1.", "(a) (b) (c) (d)",
"*** END OF PAPER ***", "ANSWER KEY (FOR TEACHERS ONLY)" must never be
translated or transliterated. Only the actual question text, options, and
instruction sentences get written in Hindi. This keeps the paper's layout
identical and correctly formatted in both languages.

Format EXACTLY like this — do not add, remove, merge, or reorder sections,
and do not change how many questions are in each section:

══════════════════════════════════════════
            {header_line}
     {subject.upper()} — {cls.upper()}
  Time: {time_allow} minutes    Max Marks: {marks}
          Difficulty: {difficulty}
══════════════════════════════════════════

General Instructions:
1. All questions are compulsory.
2. Marks for each question are indicated in brackets.
3. Read each question carefully before answering.

SECTION A — Multiple Choice Questions (1 mark each)
{section_a}

SECTION B — Short Answer Questions (2 marks each)
{section_b}

SECTION C — Long Answer Questions (5 marks each)
{section_c}

══════════════════════════════════════════
            *** END OF PAPER ***
══════════════════════════════════════════
"""

        if user_is_pro:
            prompt += """
After the paper, add:

══════════════════════════════════════════
            ANSWER KEY (FOR TEACHERS ONLY)
══════════════════════════════════════════
Provide a concise, correct answer for every single question above, in the
same order (Q1, Q2, Q3...). For MCQs, state the correct option letter and
a one-line reason. For short/long answer questions, give a model answer
of appropriate length for the marks allotted — not a one-word answer for
a 5-mark question.
"""

        prompt += "\nBase all questions strictly on the uploaded chapter content."

        pdf_bytes = pdf_file.read()
        chapter_text = extract_pdf_text(pdf_bytes)

        if chapter_text:
            # Primary path: hand Gemini the exact extracted text and forbid
            # anything else as a source. This is far more reliable than
            # trusting document-vision on the raw PDF — with a rigid format
            # template like ours, the model can otherwise lean on its own
            # training knowledge of "what a typical Class X chapter covers"
            # instead of actually reading the file. It's also lighter on
            # memory: plain text is smaller than a base64-encoded PDF blob.
            grounding_notice = f"""
════════════════════════════════════════════════════════
SOURCE MATERIAL — the ONLY content you may draw questions from.
Do not use outside knowledge of this subject/class/curriculum.
If a fact, number, or definition is not in the text below, do not
use it, even if it feels like an obvious textbook fact.
════════════════════════════════════════════════════════
{chapter_text}
════════════════════════════════════════════════════════
END OF SOURCE MATERIAL
════════════════════════════════════════════════════════
"""
            full_prompt = grounding_notice + "\n" + prompt
            parts = [{"text": full_prompt}]
        else:
            # Fallback: no extractable text layer (typically a scanned PDF).
            # Send the raw file so Gemini's document vision can still try
            # to read it — this is the old behavior, kept only as a safety net.
            pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
            parts = [
                {"inline_data": {"mime_type": "application/pdf", "data": pdf_b64}},
                {"text": prompt},
            ]
            del pdf_b64

        # Free the raw bytes as soon as possible — on a 512MB instance,
        # holding onto them alongside the outgoing request body adds up.
        del pdf_bytes

        paper_text = call_gemini(parts)
        del parts
        gc.collect()  # nudge the interpreter to release the freed buffers now

        increment_usage(db_user["email"])
        user = refresh_session_user()

        return jsonify({
            "success": True,
            "paper": paper_text,
            "papers_used": user["papers"],
            "papers_left": user["papers_left"],
            "plan": user["plan"],
        })

    except GeminiError as e:
        # Known, user-facing Gemini failure — don't count it as a used paper
        return jsonify({"success": False, "error": str(e)}), 502
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ── NEW: DOWNLOAD GENERATED PAPER AS PDF ────────
@app.route("/download-paper", methods=["POST"])
def download_paper():
    """
    Takes the generated question paper text and converts it to a downloadable PDF.
    Frontend sends: { "paper": "generated text...", "subject": "...", "cls": "...", "marks": "..." }
    """
    if "user" not in session:
        return jsonify({"success": False, "error": "Not logged in"})

    data = request.json or {}
    paper_text = data.get("paper", "")
    subject = data.get("subject", "Subject")
    cls = data.get("cls", "Class")
    marks = data.get("marks", "40")

    if not paper_text:
        return jsonify({"success": False, "error": "No paper content provided."})

    try:
        pdf_stream = generate_question_paper_pdf(paper_text, subject, cls, marks)

        # Create a safe filename
        safe_subject = re.sub(r'[^\w\s-]', '', subject).strip().replace(" ", "_")
        safe_cls = re.sub(r'[^\w\s-]', '', cls).strip().replace(" ", "_")
        filename = f"genify_{safe_subject}_{safe_cls}_{marks}marks.pdf"

        # ── Save a copy to Firebase Storage (best-effort) ──
        # This is the exact moment the PDF is created, so it's the right
        # place to hook a cloud save. Wrapped so any Firebase problem
        # (missing config, network hiccup, bad credentials) never breaks
        # the user's own download — they always get their file either way.
        try:
            email = session["user"]["email"]
            dest_path = (
                f"papers/{normalize_email(email)}/"
                f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}"
            )
            download_url = upload_pdf_to_firebase(pdf_stream.getvalue(), dest_path)
            if download_url:
                save_paper_record(email, subject, cls, marks, dest_path, download_url)
        except Exception as e:
            print(f"⚠️  Cloud save skipped: {e}")

        pdf_stream.seek(0)  # rewind — getvalue() above doesn't move the read position, but be explicit
        return send_file(
            pdf_stream,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/save-settings", methods=["POST"])
def save_settings():
    if "user" not in session:
        return jsonify({"success": False})

    data = request.json or {}
    email = session["user"]["email"]
    db_user = get_user_by_email(email)
    if not db_user:
        session.clear()
        return jsonify({"success": False, "error": "Your session has expired. Please log in again."})

    # Free users cannot save Hindi as default
    if data.get("default_language") == "Hindi" and db_user["plan"] != "Pro":
        data["default_language"] = "English"

    save_user_settings(email, data)
    refresh_session_user()
    return jsonify({"success": True})


@app.route("/admin/add-pro")
def add_pro():
    key = request.args.get("key", "")
    email = normalize_email(request.args.get("email", ""))
    plan = request.args.get("plan", "Pro")  # Pro or Starter

    if key != ADMIN_KEY:
        return "Unauthorized", 401
    if not email:
        return "Email missing.", 400

    get_or_create_user(email, "Teacher")
    set_user_plan(email, plan)

    if plan == "Pro" and email not in [e.lower() for e in PRO_USERS]:
        PRO_USERS.append(email)

    return f"✅ {email} upgraded to {plan}. No restart needed."


# ── HEALTH CHECK (handy for Render) ─────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)