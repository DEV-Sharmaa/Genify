from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
from datetime import timedelta, datetime
import google.generativeai as genai
import sqlite3
import base64
import os
import re
import io
from fpdf import FPDF

# ── CONFIG ───────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    print("⚠️  WARNING: GEMINI_API_KEY not set. Paper generation will fail.")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-production-immediately")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_SECURE"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

DB_PATH = os.path.join(os.path.dirname(__file__), "genify.db")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "genify2024")

FREE_PAPER_LIMIT = 5
STARTER_MONTHLY_LIMIT = 30

# Seed list — also stored in DB after first login
PRO_USERS = [
    "devaidomain@gmail.com",
]


# ── DATABASE ─────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
    conn.commit()
    conn.close()


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


init_db()


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


@app.route("/google-login", methods=["POST"])
def google_login():
    data = request.json or {}
    email = data.get("email", "").strip()
    name = data.get("name", "Teacher").strip()
    photo = data.get("photo", "")

    if not email:
        return jsonify({"success": False, "error": "Email required"})

    db_user = get_or_create_user(email, name, photo)
    session.permanent = True
    session["user"] = row_to_session(db_user)
    session.modified = True
    return jsonify({"success": True})


@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect(url_for("login"))
    user = refresh_session_user()
    return render_template("dashboard.html", user=user)


@app.route("/profile")
def profile():
    if "user" not in session:
        return redirect(url_for("login"))
    user = refresh_session_user()
    return render_template("profile.html", user=user)


@app.route("/pricing")
def pricing():
    user = session.get("user")
    if user:
        user = refresh_session_user()
    return render_template("pricing.html", user=user)


@app.route("/settings")
def settings():
    if "user" not in session:
        return redirect(url_for("login"))
    user = refresh_session_user()
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

        prompt = f"""You are an expert CBSE/ICSE teacher.
Generate a complete question paper in {out_lang}.

Subject: {subject} | Class: {cls} | Difficulty: {difficulty} | Total Marks: {marks}

Format EXACTLY like this:

══════════════════════════════════════════
            {header_line}
     {subject.upper()} — {cls.upper()}
  Time: {time_allow} minutes    Max Marks: {marks}
          Difficulty: {difficulty}
══════════════════════════════════════════

General Instructions:
1. All questions are compulsory.
2. Marks for each question are in brackets.
3. Write neat and clean answers.

SECTION A — Multiple Choice Questions (1 mark each)
Q1. [question] [1]
    (a) option  (b) option  (c) option  (d) option
Q2. [question] [1]
    (a) option  (b) option  (c) option  (d) option
Q3. [question] [1]
    (a) option  (b) option  (c) option  (d) option
Q4. [question] [1]
    (a) option  (b) option  (c) option  (d) option
Q5. [question] [1]
    (a) option  (b) option  (c) option  (d) option

SECTION B — Short Answer Questions (2 marks each)
Q6.  [question] [2]
Q7.  [question] [2]
Q8.  [question] [2]
Q9.  [question] [2]
Q10. [question] [2]

SECTION C — Long Answer Questions (5 marks each)
Q11. [question] [5]
Q12. [question] [5]
Q13. [question] [5]

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
Provide concise answers for every question.
"""

        prompt += "\nBase all questions strictly on the uploaded chapter content."

        pdf_bytes = pdf_file.read()
        pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

        parts = [
            {"inline_data": {"mime_type": "application/pdf", "data": pdf_b64}},
            {"text": prompt},
        ]

        response = model.generate_content(parts)
        increment_usage(db_user["email"])
        user = refresh_session_user()

        return jsonify({
            "success": True,
            "paper": response.text,
            "papers_used": user["papers"],
            "papers_left": user["papers_left"],
            "plan": user["plan"],
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


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

        return send_file(
            pdf_stream,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/save-settings", methods=["POST"])
def save_settings():
    if "user" not in session:
        return jsonify({"success": False})

    data = request.json or {}
    email = session["user"]["email"]

    # Free users cannot save Hindi as default
    if data.get("default_language") == "Hindi":
        db_user = get_user_by_email(email)
        if db_user["plan"] != "Pro":
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
