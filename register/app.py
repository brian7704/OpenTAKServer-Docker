import io
import hmac
import os
import re
import secrets
import sqlite3
import smtplib
import ssl
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from datetime import timedelta
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from admin_auth import AdminAuth

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "requests.sqlite3"
PACKAGE_DIR = DATA_DIR / "packages"
OTS_BASE_URL = os.getenv("OTS_BASE_URL", "http://opentakserver:8081").rstrip("/")
VERIFY_TLS = os.getenv("OTS_VERIFY_TLS", "false").lower() == "true"
PACKAGE_TTL = int(os.getenv("PACKAGE_TTL_HOURS", "72")) * 3600
ADMIN_SESSION_TTL = int(os.getenv("ADMIN_SESSION_MINUTES", "20")) * 60
APPLICANT_SESSION_TTL = int(os.getenv("APPLICANT_SESSION_MINUTES", "15")) * 60

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(
    APPLICATION_ROOT="/",
    SESSION_COOKIE_NAME="ots_register_admin",
    SESSION_COOKIE_PATH="/",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Strict",
    PERMANENT_SESSION_LIFETIME=timedelta(seconds=ADMIN_SESSION_TTL),
    MAX_CONTENT_LENGTH=32 * 1024,
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
limiter = Limiter(get_remote_address, app=app, default_limits=["120 per hour"], storage_uri="memory://")
fernet = Fernet(os.environ["FERNET_KEY"].encode())

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
CALLSIGN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{1,23}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PASSWORD_RE = re.compile(r"^(?=.{12,72}$)(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z0-9])[^@:\r\n]+$")
PASSWORD_POLICY_MESSAGE = "密碼需 12–72 字元，包含大小寫英文字母、數字及特殊符號，且不可包含 @ 或 :。"


def db():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        # Gunicorn workers and health checks can start together. Serialize
        # schema inspection and migration before any reads can race writes.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tracking_token TEXT UNIQUE NOT NULL,
                username TEXT UNIQUE NOT NULL,
                callsign TEXT UNIQUE NOT NULL,
                email TEXT,
                password_cipher BLOB,
                status TEXT NOT NULL DEFAULT 'pending',
                note TEXT NOT NULL DEFAULT '',
                package_name TEXT,
                download_token TEXT UNIQUE,
                created_at INTEGER NOT NULL,
                decided_at INTEGER
            )
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(applications)")}
        if "email" not in columns:
            conn.execute("ALTER TABLE applications ADD COLUMN email TEXT")
        if "password_hash" not in columns:
            conn.execute("ALTER TABLE applications ADD COLUMN password_hash TEXT")
        for name, definition in (("requested_group", "TEXT NOT NULL DEFAULT 'publicuser'"), ("group_mode", "TEXT NOT NULL DEFAULT 'listed'"), ("assigned_group", "TEXT")):
            if name not in columns:
                conn.execute(f"ALTER TABLE applications ADD COLUMN {name} {definition}")
        conn.execute("CREATE TABLE IF NOT EXISTS registration_groups (name TEXT PRIMARY KEY, listed INTEGER NOT NULL DEFAULT 0, description TEXT NOT NULL DEFAULT '')")
        conn.execute("INSERT OR IGNORE INTO registration_groups(name,listed) VALUES('publicuser',1)")
        for row in conn.execute("SELECT id,password_cipher FROM applications WHERE password_hash IS NULL AND password_cipher IS NOT NULL").fetchall():
            try:
                password = fernet.decrypt(row["password_cipher"]).decode()
                conn.execute("UPDATE applications SET password_hash=? WHERE id=?", (generate_password_hash(password), row["id"]))
            except (InvalidToken, UnicodeDecodeError):
                app.logger.warning("Cannot migrate applicant password hash for id %s", row["id"])


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def require_csrf():
    if not session.get("csrf_token") or not secrets.compare_digest(request.form.get("csrf_token", ""), session["csrf_token"]):
        abort(400, "表單已逾時，請重新整理後再試。")


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            admin_auth.token(session.get("admin_session", ""))
        except Exception:
            session.clear()
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapped


def ots_request(method, path, token=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authentication-Token"] = token
    response = requests.request(
        method, f"{OTS_BASE_URL}{path}", headers=headers, verify=VERIFY_TLS, timeout=30, **kwargs
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("message") or response.json().get("response", {}).get("errors")
        except Exception:
            detail = None
        body = response.text.strip().replace("\n", " ")[:240]
        raise RuntimeError(str(detail or f"OpenTAK 回應 HTTP {response.status_code}: {body}"))
    return response


def admin_token():
    return admin_auth.token(session.get("admin_session", ""))


def verify_application_login(row, password):
    """Validate an applicant without retaining their password after approval."""
    if row["status"] in {"pending", "rejected"} and row["password_hash"]:
        return check_password_hash(row["password_hash"], password)
    if row["status"] == "pending" and row["password_cipher"]:
        try:
            stored = fernet.decrypt(row["password_cipher"]).decode()
        except (InvalidToken, UnicodeDecodeError):
            return False
        return hmac.compare_digest(stored.encode(), password.encode())
    if row["status"] != "approved":
        return False
    # Older form-based provisioning HTML-escaped special password characters.
    # Authenticate both representations; never accept an account by name alone.
    import html
    candidates = list(dict.fromkeys((password, html.escape(password, quote=False))))
    for candidate in candidates:
        try:
            response = requests.post(
                f"{OTS_BASE_URL}/api/login", params={"include_auth_token": ""},
                json={"username": row["username"], "password": candidate},
                verify=VERIFY_TLS, timeout=15,
            )
            if response.status_code != 200:
                continue
            payload = response.json()
            user = payload.get("response", {}).get("user", {})
            if user.get("authentication_token"):
                return True
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            continue
    return False


def send_decision_email(row, approved, download_token=None, note=""):
    """Send a status email after the database transaction has succeeded."""
    recipient = (row["email"] or "").strip()
    if not recipient:
        return "no-email"
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    if not smtp_host:
        return "disabled"
    base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:8787").rstrip("/")
    message = EmailMessage()
    message["From"] = os.getenv("SMTP_FROM", "noreply@example.com")
    message["To"] = recipient
    brand_name = os.getenv("BRAND_NAME", "TAYA TAK")
    message["Subject"] = f"{brand_name} 申請已核准" if approved else f"{brand_name} 申請結果"
    if approved:
        message.set_content(
            f"您的 {brand_name} 申請已核准。\n\n帳號：{row['username']}\n"
            f"呼號：{row['callsign']}\n\nATAK 設定包已隨信附上。\n"
            f"也可隨時以申請帳號與密碼登入下載：\n{base_url}/lookup\n\n本信不會寄送密碼。"
        )
    else:
        message.set_content(
            f"您的 {brand_name} 申請未通過。\n\n帳號：{row['username']}\n"
            f"呼號：{row['callsign']}\n原因：{note or '未提供原因'}"
        )
    if approved:
        with db() as conn:
            package = conn.execute("SELECT package_name FROM applications WHERE id=? AND status='approved'", (row["id"],)).fetchone()
        if not package or not package["package_name"]:
            raise RuntimeError("核准設定包不存在，無法寄送附件")
        content = (PACKAGE_DIR / package["package_name"]).read_bytes()
        if not zipfile.is_zipfile(io.BytesIO(content)):
            raise RuntimeError("設定包格式無效，無法寄送附件")
        message.add_attachment(content, maintype="application", subtype="zip", filename=f"{row['callsign']}-ATAK-setup.zip")
    use_ssl = os.getenv("SMTP_SSL", "false").lower() in {"1", "true", "yes"}
    starttls = os.getenv("SMTP_STARTTLS", "false").lower() in {"1", "true", "yes"}
    if use_ssl and starttls:
        raise RuntimeError("SMTP_SSL 與 SMTP_STARTTLS 不可同時啟用")
    try:
        port = int(os.getenv("SMTP_PORT") or ("465" if use_ssl else "25"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise RuntimeError("SMTP_PORT 必須為 1–65535") from None
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    if bool(username) != bool(password):
        raise RuntimeError("SMTP_USERNAME 與 SMTP_PASSWORD 必須一起設定")
    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    options = {"context": ssl.create_default_context()} if use_ssl else {}
    with smtp_class(smtp_host, port, timeout=15, **options) as smtp:
        smtp.ehlo()
        if starttls:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if os.getenv("SMTP_USERNAME") and os.getenv("SMTP_PASSWORD"):
            smtp.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        refused = smtp.send_message(message)
        if refused:
            raise RuntimeError("SMTP 拒絕收件者")
    return "sent"


def cleanup_expired_packages():
    """Keep packages for authenticated downloads; token expiry is checked on access."""
    return


@app.context_processor
def globals_for_templates():
    def safe_color(name, fallback):
        value = os.getenv(name, fallback)
        return value if re.fullmatch(r"#[0-9A-Fa-f]{6}", value) else fallback

    logo_url = os.getenv("BRAND_LOGO_URL", "").strip()
    if logo_url and not logo_url.startswith(("/", "data:image/")):
        logo_url = ""
    support_url = os.getenv("SUPPORT_URL", "").strip()
    if support_url and not support_url.startswith(("https://", "http://")):
        support_url = ""
    return {
        "csrf_token": csrf_token,
        "public_host": os.getenv("PUBLIC_HOST", "localhost"),
        "stream_port": os.getenv("STREAM_PORT", "8089"),
        "enrollment_port": os.getenv("ENROLLMENT_PORT", "8446"),
        "brand_name": os.getenv("BRAND_NAME", "TAYA TAK"),
        "brand_short": os.getenv("BRAND_SHORT", "T")[:3],
        "brand_tagline": os.getenv("BRAND_TAGLINE", "私人 OpenTAK 服務"),
        "brand_description": os.getenv("BRAND_DESCRIPTION", "OpenTAK Server 帳號申請與 ATAK 設定包下載"),
        "brand_logo_url": logo_url,
        "brand_primary_color": safe_color("BRAND_PRIMARY_COLOR", "#3dd6e8"),
        "brand_accent_color": safe_color("BRAND_ACCENT_COLOR", "#ffba4a"),
        "support_label": os.getenv("SUPPORT_LABEL", ""),
        "support_url": support_url,
    }


@app.get("/brand.css")
def brand_css():
    primary = os.getenv("BRAND_PRIMARY_COLOR", "#3dd6e8")
    accent = os.getenv("BRAND_ACCENT_COLOR", "#ffba4a")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", primary):
        primary = "#3dd6e8"
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", accent):
        accent = "#ffba4a"
    return app.response_class(f":root{{--cyan:{primary};--amber:{accent}}}", mimetype="text/css")


@app.after_request
def security_headers(response):
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz")
def healthz():
    return {"status": "ok", "release": "2026-10-03-groups6"}


@app.get("/")
def index():
    cleanup_expired_packages()
    with db() as conn:
        groups = conn.execute("SELECT * FROM registration_groups WHERE listed=1 ORDER BY name").fetchall()
    return render_template("index_v5.html", groups=groups)


@app.route("/lookup", methods=["GET", "POST"])
@limiter.limit("20 per hour")
def lookup():
    if request.method == "POST":
        require_csrf()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with db() as conn:
            row = conn.execute("SELECT * FROM applications WHERE username=?", (username,)).fetchone()
        if not row or not verify_application_login(row, password):
            flash("帳號或密碼不正確，或此申請已無法查詢。", "error")
            return redirect(url_for("lookup"))
        session["applicant_id"] = row["id"]
        session["applicant_expires"] = int(time.time()) + APPLICANT_SESSION_TTL
        return redirect(url_for("my_application"))
    return render_template("lookup.html")


@app.get("/my-application")
def my_application():
    application_id = session.get("applicant_id")
    if not application_id or session.get("applicant_expires", 0) < time.time():
        session.pop("applicant_id", None)
        session.pop("applicant_expires", None)
        flash("查詢登入已逾時，請重新登入。", "error")
        return redirect(url_for("lookup"))
    with db() as conn:
        row = conn.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
    if not row:
        session.pop("applicant_id", None)
        session.pop("applicant_expires", None)
        abort(404)
    return render_template("status_v5.html", application=row)


@app.post("/apply")
@limiter.limit("5 per hour")
def apply():
    require_csrf()
    username = request.form.get("username", "").strip()
    callsign = request.form.get("callsign", "").strip().upper()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    if not USERNAME_RE.fullmatch(username):
        flash("帳號需為 3–32 字元，只能使用英數字、點、底線或連字號。", "error")
        return redirect(url_for("index"))
    if not CALLSIGN_RE.fullmatch(callsign):
        flash("呼號需為 2–24 字元，只能使用英數字、空格、點、底線或連字號。", "error")
        return redirect(url_for("index"))
    if not EMAIL_RE.fullmatch(email) or len(email) > 254:
        flash("請輸入有效的電子郵件地址。", "error")
        return redirect(url_for("index"))
    if not PASSWORD_RE.fullmatch(password):
        flash(PASSWORD_POLICY_MESSAGE, "error")
        return redirect(url_for("index"))
    if password != confirm:
        flash("兩次輸入的密碼不同。", "error")
        return redirect(url_for("index"))
    try:
        requested_group, group_mode = application_group(request.form)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))
    tracking = secrets.token_urlsafe(24)
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO applications (tracking_token,username,callsign,email,password_cipher,password_hash,created_at,requested_group,group_mode) VALUES (?,?,?,?,?,?,?,?,?)",
                (tracking, username, callsign, email, fernet.encrypt(password.encode()), generate_password_hash(password), int(time.time()), requested_group, group_mode),
            )
    except sqlite3.IntegrityError:
        flash("此帳號或呼號已在申請中。", "error")
        return redirect(url_for("index"))
    flash("申請已送出。請以申請帳號與密碼登入查詢審核結果。", "success")
    return redirect(url_for("lookup"))


@app.get("/status/<token>")
def status(token):
    with db() as conn:
        row = conn.execute("SELECT * FROM applications WHERE tracking_token=?", (token,)).fetchone()
    if not row:
        abort(404)
    return render_template("status_v5.html", application=row)


@app.get("/download/<token>")
@limiter.limit("20 per hour")
def download(token):
    cleanup_expired_packages()
    with db() as conn:

        row = conn.execute(
            "SELECT * FROM applications WHERE download_token=? AND status='approved'", (token,)
        ).fetchone()
    if not row or not row["package_name"]:
        abort(404)
    owner_session = (session.get("applicant_id") == row["id"] and
                     session.get("applicant_expires", 0) >= time.time())
    link_expired = not row["decided_at"] or row["decided_at"] + PACKAGE_TTL < time.time()
    if link_expired and not owner_session:
        flash("下載連結已逾時，請以申請帳號與密碼登入後下載。", "error")
        return redirect(url_for("lookup"))
    path = PACKAGE_DIR / row["package_name"]
    if not path.is_file():
        abort(410)
    return send_file(path, as_attachment=True, download_name=f"{row['callsign']}-ATAK-setup.zip")


@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def admin_login():
    if request.method == "POST":
        require_csrf()
        try:
            admin_auth.delete(session.get("admin_challenge", ""))
            challenge, method = admin_auth.begin(request.form.get("username", ""), request.form.get("password", ""))
            session.clear()
            session["admin_challenge"] = challenge
            session["admin_method"] = method
            return redirect(url_for("admin_two_factor"))
        except Exception as exc:
            flash(f"登入失敗：{exc}", "error")
    return render_template("admin_login.html")


@app.route("/admin/2fa", methods=["GET", "POST"])
@limiter.limit("20 per hour")
def admin_two_factor():
    challenge = session.get("admin_challenge", "")
    try:
        admin_auth.read(challenge, "challenge")
    except Exception:
        session.clear()
        return redirect(url_for("admin_login"))
    if request.method == "POST":
        require_csrf()
        try:
            key = admin_auth.finish(challenge, request.form.get("code", "").strip(), ADMIN_SESSION_TTL)
            session.clear()
            session["admin_session"] = key
            session.permanent = True
            return redirect(url_for("admin_queue"))
        except Exception:
            flash("驗證碼未通過，請確認驗證碼後重試。", "error")
    return render_template("admin_2fa.html", method=session.get("admin_method"))


@app.post("/admin/logout")
def admin_logout():
    require_csrf()
    admin_auth.delete(session.get("admin_session", ""))
    admin_auth.delete(session.get("admin_challenge", ""))
    session.clear()
    return redirect(url_for("index"))


@app.get("/admin")
@admin_required
def admin_queue():
    with db() as conn:
        rows = conn.execute("SELECT * FROM applications ORDER BY created_at DESC").fetchall()
        groups = conn.execute("SELECT * FROM registration_groups ORDER BY name").fetchall()
    return render_template("admin_v5.html", applications=rows, groups=groups)


@app.post("/admin/<int:application_id>/approve")
@admin_required
@limiter.limit("30 per hour")
def approve(application_id):
    require_csrf()
    with db() as conn:
        row = conn.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
    if not row or row["status"] != "pending":
        abort(409)
    try:
        password = fernet.decrypt(row["password_cipher"]).decode()
    except InvalidToken:
        flash("無法解密申請密碼，請申請人重新送出。", "error")
        return redirect(url_for("admin_queue"))
    try:
        token = admin_token()
        group_name = valid_group_name(request.form.get("assigned_group", row["requested_group"]))
        # Resolve a prior partial approval before attempting creation.
        # Authentication is the ownership check, never an error-message guess.
        # The user may already exist when a previous approval reached OpenTAK
        # but failed while downloading the package. Treat only a duplicate
        # response as idempotent; all other API errors remain visible.
        if not verify_application_login(dict(row, status="approved"), password):
          try:
            ots_request("POST", "/api/user/add", token=token, json={
                "username": row["username"], "password": password,
                "confirm_password": password, "roles": ["user"]
            })
          except RuntimeError as exc:
            if "already" not in str(exc).lower() and "exist" not in str(exc).lower() and "重複" not in str(exc):
                raise
            if not verify_application_login(dict(row, status="approved"), password):
                raise RuntimeError("既有 OpenTAK 帳號與申請密碼不符，請管理員確認帳號歸屬")

        # Certificate GET returns metadata; only the download endpoint returns ZIP.
        content = certificate_package(token, row["username"], row["callsign"])
        ensure_group(token, group_name)
        join_group(token, row["username"], group_name)
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO registration_groups(name,listed) VALUES(?,0)", (group_name,))
        package_name = f"{secrets.token_hex(16)}.zip"
        (PACKAGE_DIR / package_name).write_bytes(content)
        download_token = secrets.token_urlsafe(32)
        with db() as conn:
            conn.execute(
                "UPDATE applications SET status='approved',password_cipher=NULL,package_name=?,download_token=?,decided_at=?,assigned_group=? WHERE id=? AND status='pending'",
                (package_name, download_token, int(time.time()), group_name, application_id),
            )
        try:
            mail_state = send_decision_email(row, True, download_token=download_token)
            suffix = {
                "sent": "，已寄送設定包附件。",
                "disabled": "；未設定 SMTP，請通知申請人登入下載。",
                "no-email": "；此舊申請未留電子郵件。",
            }[mail_state]
            flash(f"已核准 {row['username']}，設定包可於登入後隨時下載{suffix}", "success")
        except Exception as mail_error:
            app.logger.exception("approval email failed")
            flash(f"已核准並產生設定包，但通知信寄送失敗：{mail_error}", "warning")
    except Exception as exc:
        flash(f"核准失敗：{exc}", "error")
    return redirect(url_for("admin_queue"))


@app.post("/admin/<int:application_id>/reject")
@admin_required
def reject(application_id):
    require_csrf()
    note = request.form.get("note", "").strip()[:200]
    with db() as conn:
        row = conn.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
        if not row or row["status"] != "pending":
            abort(409)
        conn.execute(
            "UPDATE applications SET status='rejected',password_cipher=NULL,note=?,decided_at=? WHERE id=? AND status='pending'",
            (note, int(time.time()), application_id),
        )
    if row:
        try:
            send_decision_email(row, False, note=note)
        except Exception:
            app.logger.exception("rejection email failed")
    flash("已拒絕申請並清除密碼。", "success")
    return redirect(url_for("admin_queue"))


def valid_group_name(value):
    name = value.strip()
    if not re.fullmatch(r"[\w][\w .-]{0,47}", name, flags=re.UNICODE) or name.startswith("__") or name.lower() in {"administrator", "administrators"}:
        raise ValueError("群組名稱需為 1–48 字元，限文字、數字、空格、點、底線或連字號，不可使用保留名稱。")
    return name


def application_group(form):
    selected = form.get("group", "publicuser")
    if selected == "__manual__":
        return valid_group_name(form.get("manual_group", "")), "manual"
    name = valid_group_name(selected)
    with db() as conn:
        allowed = conn.execute("SELECT 1 FROM registration_groups WHERE name=? AND listed=1", (name,)).fetchone()
    if not allowed:
        raise ValueError("請選擇公開群組，或選擇自行填寫群組名稱後提出申請。")
    return name, "listed"


def ensure_group(token, name, description=""):
    groups = ots_request("GET", "/api/groups/all", token=token).json()
    if not isinstance(groups, list):
        raise RuntimeError("OpenTAK 群組清單格式不正確")
    if not any(g.get("name") == name for g in groups):
        ots_request("POST", "/api/groups", token=token, headers={"Content-Type": "application/json"}, json={"name": name, "description": description})
    groups = ots_request("GET", "/api/groups/all", token=token).json()
    if not any(g.get("name") == name for g in groups):
        raise RuntimeError("OpenTAK 未建立指定群組")


def join_group(token, username, name):
    def memberships():
        data = ots_request("GET", "/api/users/groups", token=token, params={"username": username}).json()
        if data.get("success") is not True or not isinstance(data.get("results"), list):
            raise RuntimeError("OpenTAK 群組成員查詢失敗")
        return {m.get("direction") for m in data["results"] if m.get("group_name") == name and m.get("active")}
    current = memberships()
    for direction in ("IN", "OUT"):
        if direction not in current:
            ots_request("PUT", "/api/groups", token=token, headers={"Content-Type": "application/json"}, json={"users": [username], "group_name": name, "direction": direction})
    if not {"IN", "OUT"}.issubset(memberships()):
        raise RuntimeError("OpenTAK 未啟用指定群組的收發權限，申請保持待審核")


def certificate_package(token, username, callsign):
    def records():
        payload = ots_request("GET", "/api/certificate", token=token, params={"username": username, "per_page": 100}).json()
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise RuntimeError("OpenTAK 憑證查詢格式不正確")
        return [c for c in payload["results"] if c.get("data_package_filename") in {f"{username}.zip", f"{username}_CONFIG.zip"}]
    certs = records()
    if not certs:
        ots_request("POST", "/api/certificate", token=token, params={}, json={"username": username})
        certs = records()
    certs.sort(key=lambda c: c.get("expiration_date", ""), reverse=True)
    if not certs:
        raise RuntimeError("OpenTAK 建立憑證後未找到此帳號的設定包")
    cert = certs[0]
    package_hash = cert.get("data_package_hash", "")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", package_hash):
        raise RuntimeError("OpenTAK 憑證設定包缺少有效雜湊")
    response = ots_request("GET", "/api/data_packages/download", token=token, params={"hash": package_hash})
    import hashlib
    if hashlib.sha256(response.content).hexdigest() != package_hash.lower():
        raise RuntimeError("OpenTAK 設定包雜湊不符")
    return prepare_atak_package(response.content, username, callsign)


def prepare_atak_package(content, username, callsign):
    """Keep original certificate bytes; correct internal Docker host in ATAK prefs."""
    host = os.getenv("PUBLIC_HOST", "localhost")
    port = int(os.getenv("STREAM_PORT", "8089"))
    found = {"preferences": False, "certificate": False, "truststore": False}
    def rewrite(data, depth=0):
        if depth > 2 or len(data) > 16 * 1024 * 1024 or not zipfile.is_zipfile(io.BytesIO(data)):
            raise RuntimeError("OpenTAK 設定包格式無效")
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            if sum(i.file_size for i in source.infolist()) > 32 * 1024 * 1024:
                raise RuntimeError("OpenTAK 設定包過大")
            for item in source.infolist():
                body = source.read(item)
                if item.filename.endswith(".zip"):
                    body = rewrite(body, depth + 1)
                elif item.filename.endswith(".pref"):
                    root = ET.fromstring(body)
                    prefs = root.find("preference[@name='com.atakmap.app_preferences']")
                    streams = root.find("preference[@name='cot_streams']")
                    if prefs is None or streams is None:
                        raise RuntimeError("OpenTAK 設定包缺少 ATAK 連線偏好設定")
                    for entry in streams.findall("entry"):
                        if entry.get("key") == "connectString0": entry.text = f"{host}:{port}:ssl"
                        if entry.get("key") == "description0": entry.text = f"{os.getenv('BRAND_NAME', 'TAYA TAK')} {host}"
                    call = prefs.find("entry[@key='callsign']")
                    if call is None:
                        call = ET.SubElement(prefs, "entry", {"key": "callsign", "class": "class java.lang.String"})
                    call.text = callsign
                    for entry in prefs.findall("entry"):
                        if entry.get("key") == "atakUpdateServerUrl" and entry.text:
                            update_url = urlsplit(entry.text)
                            authority = f"{host}:{update_url.port}" if update_url.port else host
                            entry.text = urlunsplit((update_url.scheme, authority, update_url.path, update_url.query, update_url.fragment))
                    found["preferences"] = True
                    # Current ATAK packages store per-connection credentials in
                    # cot_streams; older packages use the application preferences.
                    password_entry = streams.find("entry[@key='clientPassword0']")
                    client_path = streams.find("entry[@key='certificateLocation0']")
                    if password_entry is None and client_path is None:
                        password_entry = streams.find("entry[@key='clientPassword']")
                        client_path = streams.find("entry[@key='certificateLocation']")
                    if password_entry is None and client_path is None:
                        password_entry = prefs.find("entry[@key='clientPassword']")
                        client_path = prefs.find("entry[@key='certificateLocation']")
                    if password_entry is None or client_path is None or not client_path.text or Path(client_path.text).name != f"{username}.p12":
                        raise RuntimeError("設定包憑證與申請帳號不符")
                    candidates = [i for i in source.infolist() if Path(i.filename).name == f"{username}.p12"]
                    if len(candidates) != 1:
                        raise RuntimeError("設定包缺少此帳號的用戶憑證")
                    key, cert, chain = pkcs12.load_key_and_certificates(source.read(candidates[0]), (password_entry.text or "").encode())
                    if key is None or cert is None or [v.value for v in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)] != [username]:
                        raise RuntimeError("用戶憑證名稱與申請帳號不符")
                    found["certificate"] = True
                    found["truststore"] = any(Path(i.filename).name == "truststore-root.p12" for i in source.infolist())
                    body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
                target.writestr(item, body)
        return output.getvalue()
    result = rewrite(content)
    if not all(found.values()):
        raise RuntimeError("OpenTAK 未回傳完整 ATAK 憑證設定包")
    return result


@app.post("/admin/groups")
@admin_required
def manage_group():
    require_csrf()
    try:
        name = valid_group_name(request.form.get("name", ""))
        description = request.form.get("description", "").strip()[:200]
        listed = request.form.get("listed") == "1"
        if name == "publicuser" and not listed:
            raise ValueError("publicuser 為預設公開群組，不可隱藏")
        ensure_group(admin_token(), name, description)
        with db() as conn:
            conn.execute("INSERT INTO registration_groups(name,listed,description) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET listed=excluded.listed,description=excluded.description", (name, int(listed), description))
        flash(f"已儲存群組 {name}；" + ("註冊時可下拉選擇。" if listed else "只接受手填名稱申請或管理員指派。"), "success")
    except Exception as exc:
        flash(f"群組儲存失敗：{exc}", "error")
    return redirect(url_for("admin_queue"))


@app.post("/admin/<int:application_id>/group")
@admin_required
def assign_group(application_id):
    require_csrf()
    with db() as conn:
        row = conn.execute("SELECT * FROM applications WHERE id=? AND status='approved'", (application_id,)).fetchone()
    if not row:
        abort(409)
    try:
        name = valid_group_name(request.form.get("assigned_group", ""))
        ensure_group(admin_token(), name)
        join_group(admin_token(), row["username"], name)
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO registration_groups(name,listed) VALUES(?,0)", (name,))
            conn.execute("UPDATE applications SET assigned_group=? WHERE id=?", (name, application_id))
        flash(f"已指派 {row['username']} 加入 {name}。既有群組成員資格保留。", "success")
    except Exception as exc:
        flash(f"群組指派失敗：{exc}", "error")
    return redirect(url_for("admin_queue"))


init_db()
admin_auth = AdminAuth(db, fernet, OTS_BASE_URL, VERIFY_TLS)
admin_auth.init_db()
