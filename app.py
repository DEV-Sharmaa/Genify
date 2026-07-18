from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from datetime import timedelta
import google.generativeai as genai
import base64
import os

# ── GEMINI ───────────────────────────────────────
genai.configure(api_key=os.environ.get("AQ.Ab8RN6KTBe9TAlhm-aryTfXzaLZ-mrDAF9IezvCVf7UMk6QaVg"))
model = genai.GenerativeModel("gemini-2.0-flash")

# ── APP SETUP ────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get(
    "SECRET_KEY",
    "genify-super-secret-key-2024-never-change"
)

# ── SESSION CONFIG ───────────────────────────────
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_COOKIE_SECURE']      = False
app.config['SESSION_COOKIE_HTTPONLY']    = True
app.config['SESSION_COOKIE_SAMESITE']   = 'Lax'

# ── PRO USERS ────────────────────────────────────
# Add Gmail of paying customers here manually
# After they pay cash/GPay — add their email and restart app
PRO_USERS = [
    "devaidomain@gmail.com"
    
    # "institute2@gmail.com",   ← example
]

# Free plan limits
FREE_PAPER_LIMIT = 5


def is_pro(email):
    return email.lower() in [e.lower() for e in PRO_USERS]


def get_user_plan(email):
    return "Pro" if is_pro(email) else "Free"


# ── LANDING ──────────────────────────────────────
@app.route("/")
def landing():
    if "user" in session:
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


# ── LOGIN ────────────────────────────────────────
@app.route("/login")
def login():
    if "user" in session:
        return redirect(url_for("dashboard"))
    return render_template("login.html")


# ── GOOGLE LOGIN ─────────────────────────────────
@app.route("/google-login", methods=["POST"])
def google_login():
    data  = request.json
    email = data.get("email", "")
    name  = data.get("name", "Teacher")
    plan  = get_user_plan(email)

    #------Thus will last for 30 days
    session.permanent = True
    
    session["user"] = {
        "name":    name,
        "email":   email,
        "photo":   data.get("photo", ""),
        "plan":    plan,
        "is_pro":  is_pro(email),
        "papers":  0,
        "joined":  "July 2026"  
    }
    session.modified = True
    return jsonify({"success": True})


# ── DASHBOARD ────────────────────────────────────
@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect(url_for("login"))
    # Refresh pro status every visit
    session["user"]["is_pro"] = is_pro(session["user"]["email"])
    session["user"]["plan"]   = get_user_plan(session["user"]["email"])
    session.modified = True
    return render_template("dashboard.html", user=session["user"])

#──────────PRICING─────────────────────────
@app.route("/pricing")
def pricing():
    user = session .get("user", None)
    return render_template("pricing.html", user=user)
    
# ── SETTINGS ─────────────────────────────────────
@app.route("/settings")
def settings():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("settings.html", user=session["user"])


# ── PROFILE ──────────────────────────────────────
@app.route("/profile")
def profile():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("profile.html", user=session["user"])


# ── LOGOUT ───────────────────────────────────────
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))


# ── GENERATE ─────────────────────────────────────
@app.route("/generate", methods=["POST"])
def generate():
    if "user" not in session:
        return jsonify({"success": False, "error": "Not logged in"})

    user       = session["user"]
    papers     = user.get("papers", 0)
    user_is_pro = is_pro(user["email"])

    # Check free limit
    if not user_is_pro and papers >= FREE_PAPER_LIMIT:
        return jsonify({
            "success": False,
            "error":   "free_limit_reached"
        })

    try:
        pdf_file   = request.files.get("pdf")
        subject    = request.form.get("subject", "Mathematics")
        cls        = request.form.get("cls", "Class 9")
        difficulty = request.form.get("difficulty", "Medium")
        marks      = request.form.get("marks", "40")
        language   = request.form.get("language", "English")
        time_allow = round(int(marks) * 1.5)
        out_lang   = "Hindi (Devanagari script)" if language == "Hindi" else "English"

        # Hindi only for Pro
        if language == "Hindi" and not user_is_pro:
            return jsonify({
                "success": False,
                "error":   "hindi_pro_only"
            })

        prompt = f"""You are an expert CBSE/ICSE teacher.
Generate a complete question paper in {out_lang}.

Subject: {subject} | Class: {cls} | Difficulty: {difficulty} | Total Marks: {marks}

Format EXACTLY like this:

══════════════════════════════════════════
            GENIFY — QUESTION PAPER
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

Base all questions strictly on the uploaded chapter content."""

        parts = []
        if pdf_file:
            pdf_b64 = base64.standard_b64encode(pdf_file.read()).decode("utf-8")
            parts.append({
                "inline_data": {
                    "mime_type": "application/pdf",
                    "data": pdf_b64
                }
            })
        parts.append({"text": prompt})

        response = model.generate_content(parts)

        # Increment paper count
        session["user"]["papers"] = papers + 1
        session.modified = True

        return jsonify({
            "success": True,
            "paper":   response.text,
            "papers_used": session["user"]["papers"],
            "papers_left": "unlimited" if user_is_pro else str(FREE_PAPER_LIMIT - session["user"]["papers"])
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ── SAVE SETTINGS ────────────────────────────────
@app.route("/save-settings", methods=["POST"])
def save_settings():
    if "user" not in session:
        return jsonify({"success": False})
    data = request.json
    session["user"]["institute_name"]   = data.get("institute_name", "")
    session["user"]["default_subject"]  = data.get("default_subject", "Mathematics")
    session["user"]["default_class"]    = data.get("default_class", "Class 9")
    session["user"]["default_language"] = data.get("default_language", "English")
    session.modified = True
    return jsonify({"success": True})


# ── ADD PRO USER (manual admin route) ────────────
# Visit: localhost:5000/admin/add-pro?email=someone@gmail.com&key=genify2024
@app.route("/admin/add-pro")
def add_pro():
    key   = request.args.get("key", "")
    email = request.args.get("email", "")
    if key != "genify2024":
        return "Unauthorized", 401
    if email and email not in PRO_USERS:
        PRO_USERS.append(email)
        return f"✅ {email} added to Pro users! Restart app to confirm."
    return f"⚠️ {email} already in Pro list or email missing."


app.run(debug=True)