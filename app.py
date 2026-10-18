from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

from flask import (
    Flask,
    abort,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)


ROOT = Path(__file__).resolve().parent


def load_local_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


load_local_env()

DATA_DIR = Path(os.environ.get("PERSONAL_HUB_DATA_DIR", ROOT / ".data"))
DB_PATH = Path(os.environ.get("PERSONAL_HUB_DATABASE", DATA_DIR / "personal-hub.sqlite3"))
PUBLIC_URL = os.environ.get("PERSONAL_HUB_PUBLIC_URL", "").rstrip("/")
COOKIE_SECURE = os.environ.get("PERSONAL_HUB_COOKIE_SECURE", "").lower() in {"1", "true", "yes"}

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("PERSONAL_HUB_SECRET_KEY") or "",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_secret_key() -> str:
    configured = app.config["SECRET_KEY"]
    if configured:
        return configured
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    secret_file = DATA_DIR / ".session-secret"
    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(48)
    try:
        with secret_file.open("x", encoding="utf-8") as stream:
            stream.write(value)
    except FileExistsError:
        value = secret_file.read_text(encoding="utf-8").strip()
    return value


app.config["SECRET_KEY"] = ensure_secret_key()


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def init_db() -> None:
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS profile (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                name TEXT NOT NULL DEFAULT '',
                headline TEXT NOT NULL DEFAULT '',
                bio TEXT NOT NULL DEFAULT '',
                interests TEXT NOT NULL DEFAULT '[]',
                location TEXT NOT NULL DEFAULT '',
                image_url TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS social_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                url TEXT NOT NULL,
                icon TEXT NOT NULL DEFAULT '↗',
                is_visible INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS now_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_visible INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                summary TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                period TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'In Progress',
                technologies TEXT NOT NULL DEFAULT '[]',
                content TEXT NOT NULL DEFAULT '{}',
                thumbnail_url TEXT NOT NULL DEFAULT '',
                published INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'Other',
                activity_date TEXT NOT NULL,
                project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
                published INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS thoughts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                external_url TEXT NOT NULL DEFAULT '',
                thought_date TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                published INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS timeline_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timeline_date TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
                published INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS admin_user (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                username TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_address TEXT NOT NULL,
                attempted_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(activity_date DESC);
            CREATE INDEX IF NOT EXISTS idx_timeline_date ON timeline_items(timeline_date DESC);
            """
        )
        project_columns = {row["name"] for row in db.execute("PRAGMA table_info(projects)").fetchall()}
        if "period" not in project_columns:
            db.execute("ALTER TABLE projects ADD COLUMN period TEXT NOT NULL DEFAULT ''")
        db.execute(
            "INSERT OR IGNORE INTO profile (id, name, headline, bio, interests, location, image_url, updated_at) "
            "VALUES (1, ?, ?, ?, ?, '', '', ?)",
            (
                "Renya Aoki",
                "Student · Developer · Entrepreneur",
                "Technology × Business. Building, learning, and exploring what comes next.",
                json.dumps(["Technology", "Business"], ensure_ascii=False),
                utc_now(),
            ),
        )
        password = os.environ.get("PERSONAL_HUB_ADMIN_PASSWORD", "")
        admin = db.execute("SELECT id FROM admin_user WHERE id = 1").fetchone()
        if password and not admin:
            username = os.environ.get("PERSONAL_HUB_ADMIN_USERNAME", "admin").strip()
            if not username or len(username) > 100:
                raise RuntimeError("PERSONAL_HUB_ADMIN_USERNAME は1〜100文字にしてください。")
            if len(password) < 12 or len(password) > 1024:
                raise RuntimeError("PERSONAL_HUB_ADMIN_PASSWORD は12〜1024文字にしてください。")
            db.execute(
                "INSERT INTO admin_user (id, username, password_hash, created_at) VALUES (1, ?, ?, ?)",
                (username, password_hash(password), utc_now()),
            )


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, salt_hex, digest_hex = stored.split("$", 2)
        if kind != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.before_request
def load_request_state() -> None:
    g.db = connect_db()
    if request.method == "POST":
        expected = session.get("csrf_token", "")
        supplied = request.form.get("csrf_token", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            abort(400, description="フォームの有効期限が切れました。ページを再読み込みしてください。")


@app.teardown_request
def close_db(error: Exception | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        try:
            if error is None:
                db.commit()
            else:
                db.rollback()
        finally:
            db.close()


@app.context_processor
def inject_globals() -> dict[str, object]:
    return {"csrf_token": csrf_token, "current_year": date.today().year, "site_name": "Personal Hub"}


@app.template_filter("from_json")
def template_from_json(value: str) -> list[str]:
    return parse_json_list(value)


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' https: data:; style-src 'self'; script-src 'self'; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
    )
    return response


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id"):
            return redirect(url_for("admin_login", next=request.path))
        user = g.db.execute("SELECT id, username FROM admin_user WHERE id = ?", (session["admin_id"],)).fetchone()
        if not user:
            session.clear()
            return redirect(url_for("admin_login"))
        g.admin = user
        return view(*args, **kwargs)

    return wrapped


def public_url(path: str) -> str:
    return f"{PUBLIC_URL}{path}" if PUBLIC_URL else url_for("home", _external=True) + path.removeprefix("/")


def parse_json_list(value: str) -> list[str]:
    try:
        result = json.loads(value or "[]")
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def validate_http_url(value: str, field_name: str) -> str:
    value = value.strip()
    if value and (urlparse(value).scheme not in {"http", "https"} or not urlparse(value).netloc):
        raise ValueError(f"{field_name}には http:// または https:// から始まるURLを入力してください。")
    return value


def date_value(value: str, field_name: str, allow_partial: bool = False) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name}を入力してください。")
    try:
        if allow_partial and re.fullmatch(r"\d{4}", value):
            return value
        if allow_partial and re.fullmatch(r"\d{4}-\d{2}", value):
            date.fromisoformat(value + "-01")
            return value
        return date.fromisoformat(value).isoformat()
    except ValueError:
        pass
    raise ValueError(f"{field_name}は YYYY-MM-DD 形式で入力してください。")


def project_rows(published_only: bool = False):
    clause = "WHERE published = 1" if published_only else ""
    rows = g.db.execute(f"SELECT * FROM projects {clause} ORDER BY created_at DESC, id DESC").fetchall()
    return [project_view(row) for row in rows]


def project_view(row):
    item = dict(row)
    item["technologies_list"] = parse_json_list(item.get("technologies", "[]"))
    try:
        item["details"] = json.loads(item.get("content") or "{}")
    except json.JSONDecodeError:
        item["details"] = {}
    return item


@app.route("/")
def home():
    profile = dict(g.db.execute("SELECT * FROM profile WHERE id = 1").fetchone())
    profile["interests_list"] = parse_json_list(profile.get("interests", "[]"))
    links = g.db.execute("SELECT * FROM social_links WHERE is_visible = 1 ORDER BY sort_order, id").fetchall()
    now_items = g.db.execute("SELECT * FROM now_items WHERE is_visible = 1 AND title != '' ORDER BY sort_order, id").fetchall()
    projects = [project for project in project_rows(True) if project["title"]]
    activities = g.db.execute(
        "SELECT a.*, p.title AS project_title, p.slug AS project_slug FROM activities a "
        "LEFT JOIN projects p ON p.id = a.project_id AND p.published = 1 "
        "WHERE a.published = 1 ORDER BY a.activity_date DESC, a.id DESC LIMIT 12"
    ).fetchall()
    thoughts = g.db.execute("SELECT * FROM thoughts WHERE published = 1 ORDER BY thought_date DESC, id DESC LIMIT 8").fetchall()
    timeline = g.db.execute(
        "SELECT t.*, p.title AS project_title, p.slug AS project_slug FROM timeline_items t "
        "LEFT JOIN projects p ON p.id = t.project_id AND p.published = 1 "
        "WHERE t.published = 1 ORDER BY t.timeline_date DESC, t.id DESC"
    ).fetchall()
    return render_template(
        "home.html",
        profile=profile,
        links=links,
        now_items=now_items,
        projects=projects,
        activities=activities,
        thoughts=thoughts,
        timeline=timeline,
        page_title=profile["name"] or "Personal Hub",
        page_description=(profile["bio"] or "A personal hub for projects, activities, and ideas.")[:160],
        og_image=profile["image_url"],
        canonical=public_url("/"),
    )


@app.route("/projects/<slug>")
def project_detail(slug: str):
    row = g.db.execute("SELECT * FROM projects WHERE slug = ? AND published = 1", (slug,)).fetchone()
    if not row:
        abort(404)
    project = project_view(row)
    profile = g.db.execute("SELECT name, image_url FROM profile WHERE id = 1").fetchone()
    return render_template(
        "project.html",
        project=project,
        owner_name=profile["name"],
        page_title=f"{project['title']} — {profile['name']}",
        page_description=(project["summary"] or project["title"])[:160],
        og_image=project.get("thumbnail_url") or profile["image_url"],
        canonical=public_url(url_for("project_detail", slug=slug)),
    )


@app.route("/admin")
def admin_root():
    if session.get("admin_id"):
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("admin_login"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_id"):
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        now = int(time.time())
        ip = request.remote_addr or "unknown"
        recent = g.db.execute(
            "SELECT COUNT(*) AS n FROM login_attempts WHERE ip_address = ? AND attempted_at > ?",
            (ip, now - 900),
        ).fetchone()["n"]
        g.db.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (now - 86400,))
        if recent >= 5:
            flash("ログイン試行が多すぎます。15分後にもう一度お試しください。", "error")
            return render_template("admin/login.html", page_title="ログイン"), 429
        username = request.form.get("username", "")[:100]
        password = request.form.get("password", "")
        admin = g.db.execute("SELECT * FROM admin_user WHERE username = ?", (username,)).fetchone()
        if admin and verify_password(password, admin["password_hash"]):
            g.db.execute("DELETE FROM login_attempts WHERE ip_address = ?", (ip,))
            session.clear()
            session["admin_id"] = admin["id"]
            csrf_token()
            session.permanent = True
            next_path = request.args.get("next", "")
            if not next_path.startswith("/admin") or next_path.startswith("//"):
                next_path = url_for("admin_dashboard")
            return redirect(next_path)
        g.db.execute("INSERT INTO login_attempts (ip_address, attempted_at) VALUES (?, ?)", (ip, now))
        flash("ユーザー名またはパスワードを確認してください。", "error")
    configured = g.db.execute("SELECT COUNT(*) AS n FROM admin_user").fetchone()["n"] > 0
    return render_template("admin/login.html", page_title="ログイン", configured=configured)


@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    stats = {
        "projects": g.db.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"],
        "activities": g.db.execute("SELECT COUNT(*) AS n FROM activities").fetchone()["n"],
        "links": g.db.execute("SELECT COUNT(*) AS n FROM social_links").fetchone()["n"],
        "drafts": g.db.execute("SELECT COUNT(*) AS n FROM projects WHERE published = 0").fetchone()["n"],
    }
    return render_template("admin/dashboard.html", page_title="Dashboard", stats=stats)


@app.route("/admin/profile", methods=["GET", "POST"])
@admin_required
def admin_profile():
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:120]
        headline = request.form.get("headline", "").strip()[:180]
        bio = request.form.get("bio", "").strip()[:2000]
        location = request.form.get("location", "").strip()[:120]
        try:
            image_url = validate_http_url(request.form.get("image_url", ""), "プロフィール画像URL")
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("admin_profile"))
        interests = [item.strip()[:60] for item in request.form.get("interests", "").split(",") if item.strip()][:12]
        if not name:
            flash("名前を入力してください。", "error")
            return redirect(url_for("admin_profile"))
        g.db.execute(
            "UPDATE profile SET name=?, headline=?, bio=?, interests=?, location=?, image_url=?, updated_at=? WHERE id=1",
            (name, headline, bio, json.dumps(interests, ensure_ascii=False), location, image_url, utc_now()),
        )
        flash("プロフィールを保存しました。", "success")
        return redirect(url_for("admin_profile"))
    profile = dict(g.db.execute("SELECT * FROM profile WHERE id=1").fetchone())
    profile["interests_text"] = ", ".join(parse_json_list(profile["interests"]))
    return render_template("admin/profile.html", page_title="Profile", profile=profile)


@app.route("/admin/security", methods=["GET", "POST"])
@admin_required
def admin_security():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        admin = g.db.execute("SELECT password_hash FROM admin_user WHERE id = ?", (session["admin_id"],)).fetchone()
        if not admin or not verify_password(current, admin["password_hash"]):
            flash("現在のパスワードを確認してください。", "error")
        elif len(new_password) < 12 or len(new_password) > 1024:
            flash("新しいパスワードは12〜1024文字にしてください。", "error")
        elif new_password != confirm:
            flash("新しいパスワードが一致しません。", "error")
        else:
            g.db.execute("UPDATE admin_user SET password_hash=? WHERE id=?", (password_hash(new_password), session["admin_id"]))
            flash("パスワードを更新しました。", "success")
        return redirect(url_for("admin_security"))
    return render_template("admin/security.html", page_title="Account security")


@app.route("/admin/social-links", methods=["GET", "POST"])
@admin_required
def admin_social_links():
    edit_id = request.args.get("edit", type=int)
    editing = g.db.execute("SELECT * FROM social_links WHERE id=?", (edit_id,)).fetchone() if edit_id else None
    if request.method == "POST":
        action = request.form.get("action", "save")
        item_id = request.form.get("id", type=int)
        if action == "delete" and item_id:
            g.db.execute("DELETE FROM social_links WHERE id=?", (item_id,))
            flash("リンクを削除しました。", "success")
            return redirect(url_for("admin_social_links"))
        if action == "toggle" and item_id:
            g.db.execute("UPDATE social_links SET is_visible=1-is_visible WHERE id=?", (item_id,))
            flash("公開状態を更新しました。", "success")
            return redirect(url_for("admin_social_links"))
        try:
            service = request.form.get("service_name", "").strip()[:60]
            display = request.form.get("display_name", "").strip()[:80]
            link_url = validate_http_url(request.form.get("url", ""), "URL")
            icon = request.form.get("icon", "↗").strip()[:8] or "↗"
            order = max(-10000, min(10000, int(request.form.get("sort_order", "0"))))
            if not service or not display or not link_url:
                raise ValueError("サービス名、表示名、URLを入力してください。")
        except (ValueError, TypeError) as error:
            flash(str(error), "error")
            return redirect(url_for("admin_social_links", edit=item_id) if item_id else url_for("admin_social_links"))
        values = (service, display, link_url, icon, int(request.form.get("is_visible") == "on"), order)
        if item_id:
            g.db.execute(
                "UPDATE social_links SET service_name=?, display_name=?, url=?, icon=?, is_visible=?, sort_order=? WHERE id=?",
                (*values, item_id),
            )
        else:
            g.db.execute(
                "INSERT INTO social_links (service_name, display_name, url, icon, is_visible, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                values,
            )
        flash("リンクを保存しました。", "success")
        return redirect(url_for("admin_social_links"))
    links = g.db.execute("SELECT * FROM social_links ORDER BY sort_order, id").fetchall()
    return render_template("admin/social_links.html", page_title="SNS Links", links=links, editing=editing)


SECTION_CONFIG = {
    "now": {
        "title": "Now",
        "table": "now_items",
        "sort": "sort_order, id",
        "fields": [
            {"name": "category", "label": "Category", "type": "text", "placeholder": "Building"},
            {"name": "title", "label": "Title", "type": "text", "required": True},
            {"name": "description", "label": "Description", "type": "textarea", "rows": 3},
            {"name": "sort_order", "label": "表示順", "type": "number", "default": "0"},
            {"name": "is_visible", "label": "公開する", "type": "checkbox", "default": "1"},
        ],
    },
    "projects": {
        "title": "Projects",
        "table": "projects",
        "sort": "updated_at DESC, id DESC",
        "fields": [
            {"name": "title", "label": "Title", "type": "text", "required": True},
            {"name": "slug", "label": "Slug", "type": "text", "required": True, "help": "英小文字・数字・ハイフン。例: personal-hub"},
            {"name": "summary", "label": "Summary", "type": "textarea", "rows": 3},
            {"name": "category", "label": "Category", "type": "text", "placeholder": "Development"},
            {"name": "period", "label": "Period", "type": "text", "placeholder": "2025 — Present"},
            {"name": "status", "label": "Status", "type": "select", "options": ["Planning", "In Progress", "Completed", "Archived"]},
            {"name": "technologies_text", "label": "Technology", "type": "text", "help": "カンマ区切り"},
            {"name": "thumbnail_url", "label": "Thumbnail URL", "type": "url"},
            {"name": "overview", "label": "Overview", "type": "textarea", "rows": 4},
            {"name": "background", "label": "Problem / Background", "type": "textarea", "rows": 4},
            {"name": "contribution", "label": "What I Did", "type": "textarea", "rows": 4},
            {"name": "result", "label": "Result", "type": "textarea", "rows": 4},
            {"name": "learning", "label": "What I Learned", "type": "textarea", "rows": 4},
            {"name": "links_text", "label": "Links", "type": "textarea", "rows": 3, "help": "1行に1 URL。公開リンクのみ"},
            {"name": "published", "label": "公開する", "type": "checkbox", "default": "0"},
        ],
    },
    "activities": {
        "title": "Activity",
        "table": "activities",
        "sort": "activity_date DESC, id DESC",
        "fields": [
            {"name": "activity_date", "label": "Date", "type": "date", "required": True},
            {"name": "title", "label": "Title", "type": "text", "required": True},
            {"name": "category", "label": "Category", "type": "select", "options": ["Development", "Research", "Startup", "Event", "School", "Learning", "Other"]},
            {"name": "description", "label": "Description", "type": "textarea", "rows": 3},
            {"name": "project_id", "label": "Related project", "type": "project"},
            {"name": "published", "label": "公開する", "type": "checkbox", "default": "0"},
        ],
    },
    "thoughts": {
        "title": "Thinking",
        "table": "thoughts",
        "sort": "thought_date DESC, id DESC",
        "fields": [
            {"name": "title", "label": "Title", "type": "text", "required": True},
            {"name": "summary", "label": "Summary", "type": "textarea", "rows": 3},
            {"name": "thought_date", "label": "Date", "type": "date"},
            {"name": "tags_text", "label": "Tags", "type": "text", "help": "カンマ区切り"},
            {"name": "content", "label": "Body", "type": "textarea", "rows": 8},
            {"name": "external_url", "label": "External article URL", "type": "url"},
            {"name": "published", "label": "公開する", "type": "checkbox", "default": "0"},
        ],
    },
    "timeline": {
        "title": "Timeline",
        "table": "timeline_items",
        "sort": "timeline_date DESC, id DESC",
        "fields": [
            {"name": "timeline_date", "label": "Date", "type": "text", "required": True, "placeholder": "2026 または 2026-10"},
            {"name": "title", "label": "Title", "type": "text", "required": True},
            {"name": "category", "label": "Category", "type": "text"},
            {"name": "description", "label": "Description", "type": "textarea", "rows": 3},
            {"name": "project_id", "label": "Related project", "type": "project"},
            {"name": "published", "label": "公開する", "type": "checkbox", "default": "0"},
        ],
    },
}


def row_for_form(section: str, row):
    if not row:
        return None
    item = dict(row)
    if section == "projects":
        item["technologies_text"] = ", ".join(parse_json_list(item.get("technologies", "[]")))
        details = json.loads(item.get("content") or "{}")
        item.update(details)
        links = details.get("links", [])
        item["links_text"] = "\n".join(link.get("url", "") for link in links if isinstance(link, dict))
    if section == "thoughts":
        item["tags_text"] = ", ".join(parse_json_list(item.get("tags", "[]")))
    return item


@app.route("/admin/<section>", methods=["GET", "POST"])
@admin_required
def admin_section(section: str):
    config = SECTION_CONFIG.get(section)
    if not config:
        abort(404)
    edit_id = request.args.get("edit", type=int)
    if request.method == "POST":
        action = request.form.get("action", "save")
        item_id = request.form.get("id", type=int)
        table = config["table"]
        if action == "delete" and item_id:
            g.db.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
            flash("項目を削除しました。", "success")
            return redirect(url_for("admin_section", section=section))
        if action == "toggle" and item_id:
            column = "is_visible" if section == "now" else "published"
            g.db.execute(f"UPDATE {table} SET {column}=1-{column} WHERE id=?", (item_id,))
            flash("公開状態を更新しました。", "success")
            return redirect(url_for("admin_section", section=section))
        try:
            values: dict[str, object] = {}
            for field in config["fields"]:
                name, kind = field["name"], field["type"]
                if kind == "checkbox":
                    values[name] = int(request.form.get(name) == "on")
                elif kind == "project":
                    value = request.form.get(name, "").strip()
                    values[name] = int(value) if value else None
                else:
                    values[name] = request.form.get(name, "").strip()
                    if len(str(values[name])) > 12000:
                        raise ValueError(f"{field['label']}が長すぎます。")
                    if field.get("required") and not values[name]:
                        raise ValueError(f"{field['label']}を入力してください。")
                    if kind == "select" and values[name] not in field.get("options", []):
                        raise ValueError(f"{field['label']}の選択内容を確認してください。")
            if section == "now":
                values["sort_order"] = max(-10000, min(10000, int(values.get("sort_order") or 0)))
            elif section == "projects":
                slug = str(values.get("slug", ""))
                if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
                    raise ValueError("Slugは英小文字・数字・ハイフンで入力してください。")
                collision = g.db.execute("SELECT id FROM projects WHERE slug=? AND id!=COALESCE(?, -1)", (slug, item_id)).fetchone()
                if collision:
                    raise ValueError("このSlugはすでに使われています。")
                technologies = [x.strip()[:60] for x in str(values.pop("technologies_text", "")).split(",") if x.strip()][:20]
                raw_links = str(values.pop("links_text", ""))
                detail_fields = {key: str(values.pop(key, "")) for key in ("overview", "background", "contribution", "result", "learning")}
                links = []
                for raw in raw_links.splitlines()[:10]:
                    url = validate_http_url(raw, "Project link")
                    if url:
                        links.append({"url": url})
                values["technologies"] = json.dumps(technologies, ensure_ascii=False)
                values["content"] = json.dumps({**detail_fields, "links": links}, ensure_ascii=False)
                values["thumbnail_url"] = validate_http_url(str(values.get("thumbnail_url", "")), "Thumbnail URL")
            elif section == "activities":
                values["activity_date"] = date_value(str(values["activity_date"]), "Date")
            elif section == "thoughts":
                thought_date = str(values.get("thought_date", ""))
                values["thought_date"] = date_value(thought_date, "Date") if thought_date else ""
                values["external_url"] = validate_http_url(str(values["external_url"]), "External URL")
                tags = [x.strip()[:60] for x in str(values.pop("tags_text", "")).split(",") if x.strip()][:20]
                values["tags"] = json.dumps(tags, ensure_ascii=False)
            elif section == "timeline":
                values["timeline_date"] = date_value(str(values["timeline_date"]), "Date", allow_partial=True)
            if section in {"projects", "activities"}:
                project_id = values.get("project_id")
                if project_id and not g.db.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone():
                    raise ValueError("関連Projectを確認してください。")
            stamp = utc_now()
            if item_id:
                if section in {"projects", "activities", "thoughts"}:
                    values["updated_at"] = stamp
                keys = list(values)
                g.db.execute(
                    f"UPDATE {table} SET " + ", ".join(f"{key}=?" for key in keys) + " WHERE id=?",
                    [values[key] for key in keys] + [item_id],
                )
            else:
                if section in {"projects", "activities", "thoughts"}:
                    values["created_at"] = stamp
                    values["updated_at"] = stamp
                keys = list(values)
                g.db.execute(
                    f"INSERT INTO {table} (" + ", ".join(keys) + ") VALUES (" + ", ".join("?" for _ in keys) + ")",
                    [values[key] for key in keys],
                )
            flash("内容を保存しました。", "success")
            return redirect(url_for("admin_section", section=section))
        except (ValueError, TypeError, sqlite3.IntegrityError) as error:
            flash(str(error) or "入力内容を確認してください。", "error")
            return redirect(url_for("admin_section", section=section, edit=item_id) if item_id else url_for("admin_section", section=section))
    editing = g.db.execute(f"SELECT * FROM {config['table']} WHERE id=?", (edit_id,)).fetchone() if edit_id else None
    editing = row_for_form(section, editing)
    rows = g.db.execute(f"SELECT * FROM {config['table']} ORDER BY {config['sort']}").fetchall()
    projects = g.db.execute("SELECT id, title FROM projects ORDER BY title").fetchall() if section in {"activities", "timeline"} else []
    return render_template(
        "admin/section.html",
        page_title=config["title"],
        section=section,
        config=config,
        rows=rows,
        editing=editing,
        projects=projects,
    )


@app.route("/robots.txt")
def robots():
    body = "User-agent: *\nAllow: /\nDisallow: /admin\n"
    body += f"Sitemap: {public_url('/sitemap.xml')}\n"
    return make_response(body, 200, {"Content-Type": "text/plain; charset=utf-8"})


@app.route("/sitemap.xml")
def sitemap():
    urls = [public_url("/")]
    for row in g.db.execute("SELECT slug FROM projects WHERE published=1"):
        urls.append(public_url(url_for("project_detail", slug=row["slug"])))
    namespace = "http://www.sitemaps.org/schemas/sitemap/0.9"
    ElementTree.register_namespace("", namespace)
    root = ElementTree.Element(f"{{{namespace}}}urlset")
    for url in urls:
        entry = ElementTree.SubElement(root, f"{{{namespace}}}url")
        ElementTree.SubElement(entry, f"{{{namespace}}}loc").text = url
    xml = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    return make_response(xml, 200, {"Content-Type": "application/xml; charset=utf-8"})


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", page_title="Not found", code=404, message="ページが見つかりませんでした。"), 404


@app.errorhandler(400)
def bad_request(error):
    return render_template("error.html", page_title="Bad request", code=400, message=getattr(error, "description", "リクエストを確認してください。")), 400


init_db()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
