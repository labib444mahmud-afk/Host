import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
import zipfile
from pathlib import Path
from threading import Lock

import psutil
import telebot
from telebot import types

# ============================================================
# LABIB PREMIUM HOSTING BOT
# Fresh Railway-ready bot.py
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = Path(os.getenv("DATA_ROOT", str(BASE_DIR / "data"))).resolve()
DEPLOY_DIR = DATA_ROOT / "deployed_bots"
LOG_DIR = DATA_ROOT / "logs"
DB_FILE = DATA_ROOT / "hosting.db"

for p in (DATA_ROOT, DEPLOY_DIR, LOG_DIR):
    p.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_MB = max(1, int(os.getenv("MAX_UPLOAD_MB", "50")))
MAX_ZIP_EXTRACT_MB = max(1, int(os.getenv("MAX_ZIP_EXTRACT_MB", "200")))
MAX_ZIP_FILES = max(10, int(os.getenv("MAX_ZIP_FILES", "1000")))
DEFAULT_USER_LIMIT = max(1, int(os.getenv("DEFAULT_USER_LIMIT", "2")))
MAX_CONFIGURABLE_LIMIT = max(DEFAULT_USER_LIMIT, int(os.getenv("MAX_CONFIGURABLE_LIMIT", "100")))


def load_local_env():
    """Optional local compatibility. Railway should use Variables."""
    values = {}
    for path in (BASE_DIR / ".env", BASE_DIR / "l.env"):
        if not path.exists():
            continue
        try:
            for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
        except OSError:
            pass
    return values


LOCAL_ENV = load_local_env()
BOT_TOKEN = os.getenv("BOT_TOKEN") or LOCAL_ENV.get("BOT_TOKEN")
OWNER_RAW = os.getenv("OWNER_USER_ID") or LOCAL_ENV.get("OWNER_USER_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID") or LOCAL_ENV.get("CHANNEL_ID", "")
OWNER_CONTACT = os.getenv("OWNER_CONTACT") or LOCAL_ENV.get("OWNER_CONTACT", "")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add BOT_TOKEN in Railway Variables.")
try:
    OWNER_ID = int(OWNER_RAW or "0")
except ValueError:
    raise RuntimeError("OWNER_USER_ID must be a numeric Telegram user ID.")
if OWNER_ID <= 0:
    raise RuntimeError("OWNER_USER_ID is missing or invalid.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
DB_LOCK = Lock()
running_processes = {}
process_meta = {}

DEFAULT_SETTINGS = {
    "maintenance": "0",
    "welcome_video": "",
    "default_limit": str(DEFAULT_USER_LIMIT),
}


# ----------------------------- DB -----------------------------

def db_connect():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with DB_LOCK, db_connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                file_limit INTEGER NOT NULL DEFAULT 2,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                root TEXT NOT NULL,
                entry TEXT NOT NULL,
                kind TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE(user_id, name),
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        for key, value in DEFAULT_SETTINGS.items():
            db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))
        db.commit()


def get_setting(key, default=None):
    with DB_LOCK, db_connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with DB_LOCK, db_connect() as db:
        db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        db.commit()


def ensure_user(user):
    uid = int(user.id)
    now = int(time.time())
    default_limit = int(get_setting("default_limit", DEFAULT_USER_LIMIT))
    with DB_LOCK, db_connect() as db:
        row = db.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
        if row is None:
            limit = -1 if uid == OWNER_ID else default_limit
            db.execute(
                "INSERT INTO users(user_id,first_name,username,file_limit,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (uid, user.first_name or "", user.username or "", limit, now, now),
            )
        else:
            db.execute(
                "UPDATE users SET first_name=?, username=?, updated_at=? WHERE user_id=?",
                (user.first_name or "", user.username or "", now, uid),
            )
        db.commit()


def user_limit(uid):
    if int(uid) == OWNER_ID:
        return -1
    with DB_LOCK, db_connect() as db:
        row = db.execute("SELECT file_limit FROM users WHERE user_id=?", (int(uid),)).fetchone()
    return int(row["file_limit"]) if row else DEFAULT_USER_LIMIT


def set_user_limit(uid, limit):
    uid = int(uid)
    now = int(time.time())
    with DB_LOCK, db_connect() as db:
        row = db.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO users(user_id,first_name,username,file_limit,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (uid, "", "", -1 if uid == OWNER_ID else int(limit), now, now),
            )
        else:
            db.execute("UPDATE users SET file_limit=?, updated_at=? WHERE user_id=?", (int(limit), now, uid))
        db.commit()


def project_count(uid):
    with DB_LOCK, db_connect() as db:
        return int(db.execute("SELECT COUNT(*) FROM projects WHERE user_id=?", (int(uid),)).fetchone()[0])


def get_projects(uid):
    with DB_LOCK, db_connect() as db:
        return db.execute("SELECT * FROM projects WHERE user_id=? ORDER BY id DESC", (int(uid),)).fetchall()


def get_project(uid, name):
    with DB_LOCK, db_connect() as db:
        return db.execute(
            "SELECT * FROM projects WHERE user_id=? AND name=?", (int(uid), name)
        ).fetchone()


def add_project(uid, name, root, entry, kind):
    with DB_LOCK, db_connect() as db:
        db.execute(
            "INSERT INTO projects(user_id,name,root,entry,kind,created_at) VALUES(?,?,?,?,?,?)",
            (int(uid), name, root, entry, kind, int(time.time())),
        )
        db.commit()


def delete_project_record(uid, name):
    with DB_LOCK, db_connect() as db:
        db.execute("DELETE FROM projects WHERE user_id=? AND name=?", (int(uid), name))
        db.commit()


def all_users():
    with DB_LOCK, db_connect() as db:
        return db.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()


# --------------------------- Security -------------------------

def safe_name(name):
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip(".")
    if not name or name in {".", ".."}:
        raise ValueError("Invalid file name")
    return name[:180]


def user_root(uid):
    root = DEPLOY_DIR / str(int(uid))
    root.mkdir(parents=True, exist_ok=True)
    return root


def is_within(child, parent):
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def safe_extract(zf, dest):
    dest = dest.resolve()
    total = 0
    count = 0
    for info in zf.infolist():
        count += 1
        if count > MAX_ZIP_FILES:
            raise ValueError(f"ZIP has too many files. Maximum: {MAX_ZIP_FILES}")
        if info.file_size < 0:
            raise ValueError("Invalid ZIP entry")
        total += info.file_size
        if total > MAX_ZIP_EXTRACT_MB * 1024 * 1024:
            raise ValueError(f"ZIP expands beyond {MAX_ZIP_EXTRACT_MB} MB")
        member = Path(info.filename)
        if member.is_absolute() or ".." in member.parts:
            raise ValueError(f"Unsafe ZIP path: {info.filename}")
        target = (dest / member).resolve()
        if not is_within(target, dest):
            raise ValueError(f"Unsafe ZIP path: {info.filename}")
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)


# ------------------------ Project detect -----------------------

def find_entrypoint(root):
    package = root / "package.json"
    if package.exists():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
            if data.get("scripts", {}).get("start"):
                return None, "node-package"
            main = data.get("main")
            if main and (root / main).is_file():
                return root / main, "node"
        except Exception:
            pass

    preferred = ["bot.py", "main.py", "app.py", "run.py", "index.js", "bot.js", "main.js", "server.js"]
    for name in preferred:
        matches = sorted(p for p in root.rglob(name) if p.is_file())
        if matches:
            return matches[0], "python" if matches[0].suffix == ".py" else "node"

    py = sorted(p for p in root.rglob("*.py") if p.is_file() and ".venv" not in p.parts)
    if py:
        return py[0], "python"
    js = sorted(p for p in root.rglob("*.js") if p.is_file() and "node_modules" not in p.parts)
    if js:
        return js[0], "node"
    return None, None


IMPORT_MAP = {
    "telebot": "pyTelegramBotAPI",
    "telegram": "python-telegram-bot",
    "requests": "requests",
    "aiohttp": "aiohttp",
    "httpx": "httpx",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python-headless",
    "PIL": "Pillow",
    "dotenv": "python-dotenv",
    "yaml": "PyYAML",
    "dateutil": "python-dateutil",
    "pytz": "pytz",
    "flask": "Flask",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "psutil": "psutil",
}
PY_STDLIB = set(
    "abc argparse asyncio base64 collections csv datetime decimal email enum functools "
    "hashlib html http inspect io itertools json logging math multiprocessing os pathlib "
    "pickle platform random re secrets shutil signal socket sqlite3 statistics string "
    "subprocess sys tempfile textwrap threading time traceback typing unittest urllib uuid "
    "warnings weakref xml zipfile zlib"
    .split()
)


def detect_imports(root):
    found = set()
    for path in root.rglob("*.py"):
        if ".venv" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in re.finditer(
            r"^\s*(?:from\s+([A-Za-z_][\w]*)|import\s+([A-Za-z_][\w]*))",
            text,
            re.MULTILINE,
        ):
            found.add(match.group(1) or match.group(2))
    return sorted(found)


def install_python_dependencies(root):
    req = root / "requirements.txt"
    venv = root / ".venv"
    if not venv.exists():
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
    py = venv / "bin" / "python"
    if not py.exists():
        py = venv / "Scripts" / "python.exe"
    if not py.exists():
        raise RuntimeError("Python virtual environment could not be created")

    if req.exists():
        result = subprocess.run(
            [str(py), "-m", "pip", "install", "-r", str(req)],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-6000:] or "pip install failed")
    else:
        local_names = {p.stem for p in root.rglob("*.py")}
        packages = []
        for mod in detect_imports(root):
            if mod in PY_STDLIB or mod in local_names or mod.startswith("_"):
                continue
            package = IMPORT_MAP.get(mod)
            if package and package not in packages:
                packages.append(package)
        if packages:
            result = subprocess.run(
                [str(py), "-m", "pip", "install", *packages],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr[-6000:] or "dependency install failed")
    return str(py)


# ----------------------- Process control -----------------------

def project_key(project):
    root = DEPLOY_DIR / project["root"]
    return str((root / project["entry"]).resolve())


def stop_process(key):
    proc = running_processes.get(key)
    if not proc:
        return False
    try:
        if proc.poll() is None:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=7)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.terminate()
                proc.wait(timeout=7)
    except (ProcessLookupError, OSError):
        pass
    finally:
        running_processes.pop(key, None)
        meta = process_meta.pop(key, None)
        if meta and meta.get("log"):
            try:
                with open(meta["log"], "a", encoding="utf-8") as f:
                    f.write(f"\n--- STOP {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            except OSError:
                pass
    return True


def is_running(project):
    key = project_key(project)
    proc = running_processes.get(key)
    if proc and proc.poll() is None:
        return True
    if proc:
        running_processes.pop(key, None)
        process_meta.pop(key, None)
    return False


def read_log(project, limit=5000):
    uid = int(project["user_id"])
    path = LOG_DIR / f"{uid}_{safe_name(project['name'])}.log"
    if not path.exists():
        return "No logs yet."
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[-limit:] or "No logs yet."
    except OSError as exc:
        return f"Could not read log: {exc}"


def run_project(project):
    uid = int(project["user_id"])
    root = DEPLOY_DIR / project["root"]
    entry = root / project["entry"]
    kind = project["kind"]

    if not root.exists() or not entry.exists():
        return False, "Project files are missing."

    key = project_key(project)
    old = running_processes.get(key)
    if old and old.poll() is None:
        return True, "Already running."
    running_processes.pop(key, None)
    process_meta.pop(key, None)

    app_root = root / "app" if (root / "app").exists() else root
    try:
        if kind == "python":
            interpreter = install_python_dependencies(app_root)
            cmd = [interpreter, str(entry)]
        elif kind == "node":
            if shutil.which("node") is None:
                return False, "Node.js is not installed in this Railway container."
            if (app_root / "package.json").exists() and not (app_root / "node_modules").exists():
                result = subprocess.run(
                    ["npm", "install", "--omit=dev"],
                    cwd=str(app_root),
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                if result.returncode != 0:
                    return False, result.stderr[-6000:] or "npm install failed."
            cmd = ["node", str(entry)]
        elif kind == "node-package":
            if shutil.which("npm") is None:
                return False, "npm is not installed in this Railway container."
            result = subprocess.run(
                ["npm", "install", "--omit=dev"],
                cwd=str(app_root),
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0:
                return False, result.stderr[-6000:] or "npm install failed."
            cmd = ["npm", "start"]
        else:
            return False, "Unsupported project type."
    except subprocess.TimeoutExpired:
        return False, "Dependency installation timed out."
    except Exception as exc:
        return False, f"Dependency setup error: {exc}"

    log_path = LOG_DIR / f"{uid}_{safe_name(project['name'])}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_file = log_path.open("a", encoding="utf-8")
    except OSError as exc:
        return False, f"Could not open log file: {exc}"

    log_file.write(f"\n--- START {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    log_file.write("COMMAND: " + " ".join(map(str, cmd)) + "\n")
    log_file.flush()

    blocked = {
        "BOT_TOKEN", "OWNER_USER_ID", "CHANNEL_ID", "OWNER_CONTACT",
        "RAILWAY_TOKEN", "RAILWAY_API_TOKEN", "DATABASE_URL",
    }
    child_env = {
        k: v for k, v in os.environ.items()
        if k not in blocked and not k.upper().endswith("_TOKEN")
    }
    child_env["HOSTING_USER_ID"] = str(uid)

    kwargs = {
        "cwd": str(app_root),
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "text": True,
        "env": child_env,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception as exc:
        log_file.close()
        return False, f"Could not start process: {exc}"

    # Give the program a short startup window so immediate crashes are reported.
    time.sleep(2.5)
    code = proc.poll()
    if code is not None:
        log_file.close()
        detail = read_log(project)
        return False, f"Process crashed/exited with code {code}.\n\n{detail[-4500:]}"

    running_processes[key] = proc
    process_meta[key] = {
        "user_id": uid,
        "project": project["name"],
        "log": str(log_path),
        "started": int(time.time()),
        "pid": proc.pid,
        "file": log_file,
    }
    return True, "Running"


# --------------------------- UI helpers -------------------------

def answer(call, text="", alert=False):
    try:
        bot.answer_callback_query(call.id, text=text[:190] if text else "", show_alert=alert)
    except Exception:
        pass


def edit_or_send(message, text, markup=None):
    """Safe message editor. Fixes Telegram 400 'there is no text in the message'."""
    try:
        # Video/photo messages have captions, not text.
        content_type = getattr(message, "content_type", "text")
        if content_type in {"video", "photo", "animation", "document", "audio", "voice"}:
            if content_type == "video":
                bot.edit_message_caption(
                    caption=text,
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_markup=markup,
                )
            elif content_type == "photo":
                bot.edit_message_caption(
                    caption=text,
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_markup=markup,
                )
            else:
                # For media where a caption may not exist, send a fresh text message.
                raise RuntimeError("media-message-no-text")
        else:
            bot.edit_message_text(
                text,
                chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=markup,
            )
        return message
    except Exception as exc:
        # If Telegram rejects an edit because the original message has no text/caption,
        # create a new text message instead of showing an API error to the user.
        logging.warning("safe edit failed: %s", exc)
        try:
            return bot.send_message(message.chat.id, text, reply_markup=markup)
        except Exception:
            return None


def main_keyboard(uid):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📤 Deploy File", callback_data="menu_deploy"),
        types.InlineKeyboardButton("📂 My Files", callback_data="menu_files"),
    )
    kb.add(
        types.InlineKeyboardButton("📊 Statistics", callback_data="menu_stats"),
        types.InlineKeyboardButton("ℹ️ Hosting Info", callback_data="menu_info"),
    )
    kb.add(
        types.InlineKeyboardButton("📢 Updates", callback_data="menu_updates"),
        types.InlineKeyboardButton("📞 Contact Owner", callback_data="menu_contact"),
    )
    if int(uid) == OWNER_ID:
        kb.add(types.InlineKeyboardButton("👑 Admin Panel", callback_data="menu_admin"))
    return kb


def home_keyboard():
    return types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("🏠 Home", callback_data="home")
    )


def admin_keyboard():
    maintenance = get_setting("maintenance", "0") == "1"
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("👥 Users & Limits", callback_data="adm_users"),
        types.InlineKeyboardButton("🌍 All Files", callback_data="adm_files"),
    )
    kb.add(
        types.InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast"),
        types.InlineKeyboardButton("🎥 Welcome Video", callback_data="adm_video"),
    )
    kb.add(
        types.InlineKeyboardButton(
            "🔴 Maintenance ON" if maintenance else "🟢 Maintenance OFF",
            callback_data="adm_maintenance",
        ),
        types.InlineKeyboardButton("🖥 Server Stats", callback_data="adm_stats"),
    )
    kb.add(types.InlineKeyboardButton("🏠 Home", callback_data="home"))
    return kb


def project_keyboard(uid, name, owner_view=False):
    # Callback names are intentionally compact; file names are validated before storage.
    uid = int(uid)
    return types.InlineKeyboardMarkup(row_width=2).add(
        types.InlineKeyboardButton("▶️ RUN", callback_data=f"run|{uid}|{name}"),
        types.InlineKeyboardButton("⏹ STOP", callback_data=f"stop|{uid}|{name}"),
        types.InlineKeyboardButton("📜 LOG", callback_data=f"log|{uid}|{name}"),
        types.InlineKeyboardButton("📥 DOWNLOAD", callback_data=f"down|{uid}|{name}"),
        types.InlineKeyboardButton("🗑 DELETE", callback_data=f"del|{uid}|{name}"),
        types.InlineKeyboardButton("🏠 Home", callback_data="home"),
    )


def limit_label(limit):
    return "∞ Unlimited" if int(limit) < 0 else str(int(limit))


def welcome_text(user):
    uid = int(user.id)
    ensure_user(user)
    used = project_count(uid)
    limit = limit_label(user_limit(uid))
    maintenance = "🔴 Maintenance" if get_setting("maintenance", "0") == "1" else "🟢 Online"
    return (
        "┏━━━━━━━━━━━━━━━━━━━━━━┓\n"
        "┃ ⚡ *PREMIUM HOSTING* ⚡ ┃\n"
        "┗━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
        f"👋 Welcome, *{user.first_name or 'User'}*!\n\n"
        "🚀 Python / JavaScript / ZIP hosting\n"
        "📦 Automatic dependency setup\n"
        "📜 Project logs & process control\n"
        "🛡️ Upload & ZIP safety checks\n\n"
        f"👤 ID: `{uid}`\n"
        f"📂 Projects: *{used}/{limit}*\n"
        f"⚡ Status: *{maintenance}*\n\n"
        "👇 Choose an option below:"
    )


# ------------------------ Start / upload -----------------------
@bot.message_handler(commands=["start"])
def start(message):
    ensure_user(message.from_user)
    if get_setting("maintenance", "0") == "1" and message.from_user.id != OWNER_ID:
        bot.send_message(
            message.chat.id,
            "🔴 *Hosting is temporarily under maintenance.*",
            reply_markup=home_keyboard(),
        )
        return

    text = welcome_text(message.from_user)
    video_id = get_setting("welcome_video", "")
    if video_id:
        try:
            bot.send_video(
                message.chat.id,
                video_id,
                caption=text,
                reply_markup=main_keyboard(message.from_user.id),
            )
            return
        except Exception:
            logging.exception("Welcome video send failed; falling back to text")
    bot.send_message(message.chat.id, text, reply_markup=main_keyboard(message.from_user.id))


def start_deployment(message, edit=False):
    uid = int(message.chat.id)
    ensure_user(message.from_user if getattr(message, "from_user", None) else types.SimpleNamespace(id=uid, first_name="", username=""))
    limit = user_limit(uid)
    used = project_count(uid)

    if limit >= 0 and used >= limit:
        text = (
            "🚫 *Hosting limit reached.*\n\n"
            f"📂 Used: `{used}/{limit}`\n\n"
            "Ask the Owner to increase your project limit."
        )
        if edit:
            edit_or_send(message, text, home_keyboard())
        else:
            bot.send_message(message.chat.id, text, reply_markup=home_keyboard())
        return

    text = (
        "📤 *Upload your project now*\n\n"
        "Send ONE `.py`, `.js`, or `.zip` file as a Telegram document.\n\n"
        f"📂 Current usage: `{used}/{limit_label(limit)}`\n"
        f"📦 Maximum upload: `{MAX_UPLOAD_MB} MB`\n\n"
        "⚠️ Upload only code you trust."
    )

    if edit:
        waiting = edit_or_send(message, text, home_keyboard())
        if waiting:
            bot.register_next_step_handler(waiting, process_upload)
    else:
        waiting = bot.send_message(message.chat.id, text, reply_markup=home_keyboard())
        bot.register_next_step_handler(waiting, process_upload)


def process_upload(message):
    uid = int(message.from_user.id)
    ensure_user(message.from_user)

    if get_setting("maintenance", "0") == "1" and uid != OWNER_ID:
        bot.send_message(message.chat.id, "🔴 Hosting is under maintenance.", reply_markup=home_keyboard())
        return

    # Only documents are accepted. Buttons/text sent during the waiting state are handled safely.
    if not getattr(message, "document", None):
        bot.send_message(
            message.chat.id,
            "❌ Please send a `.py`, `.js`, or `.zip` file as a Telegram *document*.\n\n"
            "Tap Deploy File again if you want to cancel this upload step.",
            reply_markup=home_keyboard(),
        )
        return

    limit = user_limit(uid)
    used = project_count(uid)
    if limit >= 0 and used >= limit:
        bot.send_message(
            message.chat.id,
            f"🚫 *Limit reached:* `{used}/{limit}` projects.",
            reply_markup=home_keyboard(),
        )
        return

    original = safe_name(message.document.file_name or "project")
    ext = Path(original).suffix.lower()
    if ext not in {".py", ".js", ".zip"}:
        bot.send_message(message.chat.id, "❌ Supported files: `.py`, `.js`, `.zip`", reply_markup=home_keyboard())
        return

    if message.document.file_size and message.document.file_size > MAX_UPLOAD_MB * 1024 * 1024:
        bot.send_message(
            message.chat.id,
            f"❌ File is larger than `{MAX_UPLOAD_MB} MB`.",
            reply_markup=home_keyboard(),
        )
        return

    base = Path(original).stem
    base = safe_name(base or "project")
    bot_dir = user_root(uid) / f"{base}_{int(time.time() * 1000)}"
    bot_dir.mkdir(parents=True, exist_ok=False)
    progress = bot.send_message(message.chat.id, "⏳ *Downloading and preparing your project...*")

    try:
        info = bot.get_file(message.document.file_id)
        content = bot.download_file(info.file_path)
        if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
            raise ValueError(f"File is larger than {MAX_UPLOAD_MB} MB")

        if ext == ".zip":
            archive = bot_dir / original
            archive.write_bytes(content)
            with zipfile.ZipFile(archive) as zf:
                bad = zf.testzip()
                if bad:
                    raise ValueError(f"Corrupt ZIP entry: {bad}")
                app_dir = bot_dir / "app"
                app_dir.mkdir(parents=True, exist_ok=True)
                safe_extract(zf, app_dir)
            entry, kind = find_entrypoint(bot_dir / "app")
            if not entry:
                raise ValueError("ZIP contains no supported Python/Node entrypoint")
            rel_entry = str(entry.relative_to(bot_dir))
            name = original
        else:
            entry = bot_dir / original
            entry.write_bytes(content)
            kind = "python" if ext == ".py" else "node"
            rel_entry = original
            name = original

        if get_project(uid, name):
            raise ValueError("A project with this file name already exists")

        project = {
            "user_id": uid,
            "name": name,
            "root": str(bot_dir.relative_to(DEPLOY_DIR)),
            "entry": rel_entry,
            "kind": kind,
        }

        # Add DB record first so crash logs can be viewed from My Files.
        add_project(uid, name, project["root"], rel_entry, kind)
        saved = get_project(uid, name)
        ok, detail = run_project(saved)
        if not ok:
            # Keep the project and its logs so the user can inspect the crash.
            text = (
                "❌ *Project uploaded, but it crashed during startup.*\n\n"
                f"📄 `{name}`\n"
                f"🔴 Status: CRASHED\n\n"
                f"```\n{detail[-3800:]}\n```"
            )
            edit_or_send(progress, text, project_keyboard(uid, name))
            return

        used = project_count(uid)
        limit_text = limit_label(user_limit(uid))
        text = (
            "🚀 *Deployment successful!*\n\n"
            f"📄 `{name}`\n"
            f"🟢 Status: RUNNING\n"
            f"🧩 Type: `{kind}`\n"
            f"📂 Projects: `{used}/{limit_text}`"
        )
        edit_or_send(progress, text, project_keyboard(uid, name))

    except Exception as exc:
        logging.exception("Upload/deployment failed")
        shutil.rmtree(bot_dir, ignore_errors=True)
        edit_or_send(
            progress,
            f"❌ *Deployment failed.*\n\n`{str(exc)[-4000:]}`",
            home_keyboard(),
        )


# -------------------------- Project UI --------------------------
def status_text(project):
    return "🟢 RUNNING" if is_running(project) else "🔴 STOPPED / CRASHED"


def show_my_files(message, edit=False):
    rows = get_projects(message.from_user.id)
    limit = limit_label(user_limit(message.from_user.id))
    header = f"📂 *My Projects*\n\nUsage: `{len(rows)}/{limit}`"
    if edit:
        edit_or_send(message, header, home_keyboard())
    else:
        bot.send_message(message.chat.id, header, reply_markup=home_keyboard())

    if not rows:
        bot.send_message(message.chat.id, "No projects yet.", reply_markup=home_keyboard())
        return

    for p in rows:
        bot.send_message(
            message.chat.id,
            f"📄 *{p['name']}*\n{status_text(p)}\n🧩 Type: `{p['kind']}`",
            reply_markup=project_keyboard(p["user_id"], p["name"]),
        )


def send_statistics(message):
    users = all_users()
    total = 0
    running = 0
    for user in users:
        projects = get_projects(user["user_id"])
        total += len(projects)
        running += sum(1 for p in projects if is_running(p))
    stopped = total - running
    bot.send_message(
        message.chat.id,
        f"📊 *Hosting Statistics*\n\n"
        f"👥 Users: `{len(users)}`\n"
        f"📦 Projects: `{total}`\n"
        f"🟢 Running: `{running}`\n"
        f"🔴 Stopped/Crash: `{stopped}`",
        reply_markup=home_keyboard(),
    )


def updates(message, edit=False):
    if not CHANNEL_ID:
        text = "📢 *Updates*\n\nUpdates channel is not configured."
        markup = home_keyboard()
    else:
        username = CHANNEL_ID.strip().lstrip("@")
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("📢 JOIN CHANNEL", url=f"https://t.me/{username}"))
        markup.add(types.InlineKeyboardButton("🏠 Home", callback_data="home"))
        text = "📢 *Stay updated*\n\nJoin our official updates channel."
    if edit:
        edit_or_send(message, text, markup)
    else:
        bot.send_message(message.chat.id, text, reply_markup=markup)


def contact_owner(message, edit=False):
    text = "📞 *Contact Owner*\n\n" + (OWNER_CONTACT or "Owner contact is not configured.")
    if edit:
        edit_or_send(message, text, home_keyboard())
    else:
        bot.send_message(message.chat.id, text, reply_markup=home_keyboard())


def project_action(call, action, project):
    uid = int(project["user_id"])
    name = project["name"]

    if action == "run":
        # Answer immediately so Telegram does not show a callback timeout while dependencies install.
        answer(call, "⏳ Starting project...")
        ok, detail = run_project(project)
        if ok:
            text = f"▶️ *{name}*\n\n🟢 Status: RUNNING\n\nThe project is running."
        else:
            text = f"▶️ *{name}*\n\n🔴 Status: CRASHED\n\n```\n{detail[-3800:]}\n```"
        bot.send_message(call.message.chat.id, text, reply_markup=project_keyboard(uid, name))
        return

    if action == "stop":
        stopped = stop_process(project_key(project))
        answer(call, "⏹ Stopped" if stopped else "Already stopped")
        bot.send_message(
            call.message.chat.id,
            f"⏹ *{name}*\n\n🔴 Status: STOPPED",
            reply_markup=project_keyboard(uid, name),
        )
        return

    if action == "log":
        answer(call, "📜 Sending logs...")
        text = read_log(project, 6500)
        # Keep Markdown safe by using a plain code block only for log text.
        bot.send_message(
            call.message.chat.id,
            f"📜 *{name} logs*\n\n```\n{text[-5500:]}\n```",
            reply_markup=project_keyboard(uid, name),
        )
        return

    if action == "down":
        answer(call, "📥 Preparing download...")
        root = DEPLOY_DIR / project["root"]
        entry = root / project["entry"]
        if not entry.exists() or not entry.is_file():
            answer(call, "File not found", True)
            return
        try:
            with entry.open("rb") as f:
                bot.send_document(call.message.chat.id, f, caption=f"📥 `{name}`")
        except Exception as exc:
            bot.send_message(call.message.chat.id, f"❌ Download failed:\n`{str(exc)[:1200]}`", reply_markup=project_keyboard(uid, name))
        return

    if action == "del":
        answer(call, "🗑 Deleting...")
        stop_process(project_key(project))
        shutil.rmtree(DEPLOY_DIR / project["root"], ignore_errors=True)
        delete_project_record(uid, name)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.send_message(call.message.chat.id, f"🗑 *{name}* deleted successfully.", reply_markup=home_keyboard())


# -------------------------- Admin UI ----------------------------
def handle_user_admin(call, target):
    target = int(target)
    limit = "∞" if target == OWNER_ID or user_limit(target) < 0 else str(user_limit(target))
    used = project_count(target)
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("➕ Limit +1", callback_data=f"limadd|{target}"),
        types.InlineKeyboardButton("➖ Limit -1", callback_data=f"limsub|{target}"),
    )
    kb.add(
        types.InlineKeyboardButton("🔢 Set Limit", callback_data=f"limset|{target}"),
        types.InlineKeyboardButton("♾️ Unlimited", callback_data=f"limunlimited|{target}"),
    )
    kb.add(types.InlineKeyboardButton("🔄 Default (2)", callback_data=f"limdefault|{target}"))
    kb.add(types.InlineKeyboardButton("⬅️ Users", callback_data="adm_users"))
    bot.send_message(
        call.message.chat.id,
        f"👤 User: `{target}`\n📂 Projects: `{used}/{limit}`",
        reply_markup=kb,
    )


def set_limit_logic(message, target):
    if message.from_user.id != OWNER_ID:
        return
    raw = (message.text or "").strip().lower()
    try:
        new_limit = -1 if raw == "unlimited" else int(raw)
        if new_limit < -1 or new_limit > MAX_CONFIGURABLE_LIMIT:
            raise ValueError
        if int(target) == OWNER_ID:
            new_limit = -1
        set_user_limit(target, new_limit)
        bot.send_message(
            message.chat.id,
            f"✅ User `{target}` limit set to `{'Unlimited' if new_limit < 0 else new_limit}`.",
            reply_markup=admin_keyboard(),
        )
    except Exception:
        bot.send_message(
            message.chat.id,
            f"❌ Enter `0-{MAX_CONFIGURABLE_LIMIT}` or `unlimited`.",
            reply_markup=admin_keyboard(),
        )


def broadcast_logic(message):
    if message.from_user.id != OWNER_ID:
        return
    text = (message.text or "").strip()
    if not text:
        bot.send_message(message.chat.id, "❌ Broadcast text is empty.", reply_markup=admin_keyboard())
        return
    sent = failed = 0
    for user in all_users():
        try:
            bot.send_message(user["user_id"], f"📢 *Announcement*\n\n{text}")
            sent += 1
        except Exception:
            failed += 1
    bot.send_message(message.chat.id, f"✅ Sent: `{sent}`\n❌ Failed: `{failed}`", reply_markup=admin_keyboard())


def save_video_logic(message):
    if message.from_user.id != OWNER_ID:
        return
    if (message.text or "").strip().upper() == "REMOVE":
        set_setting("welcome_video", "")
        bot.send_message(message.chat.id, "✅ Welcome video removed.", reply_markup=admin_keyboard())
        return
    if message.video:
        set_setting("welcome_video", message.video.file_id)
        bot.send_message(message.chat.id, "✅ Welcome video saved.", reply_markup=admin_keyboard())
        return
    bot.send_message(
        message.chat.id,
        "❌ Please send a video or type `REMOVE`.",
        reply_markup=admin_keyboard(),
    )


def handle_admin_callback(call, data):
    if data == "adm_maintenance":
        new = "0" if get_setting("maintenance", "0") == "1" else "1"
        set_setting("maintenance", new)
        edit_or_send(call.message, "👑 *Admin Control Center*", admin_keyboard())
        answer(call, "Updated")
        return

    if data == "adm_stats":
        cpu = psutil.cpu_percent(interval=0.2)
        ram = psutil.virtual_memory().percent
        disk = psutil.disk_usage(str(DATA_ROOT)).percent
        answer(call, f"CPU {cpu}% | RAM {ram}% | Disk {disk}%", True)
        return

    if data == "adm_users":
        users = all_users()
        if not users:
            bot.send_message(call.message.chat.id, "No users yet.", reply_markup=admin_keyboard())
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        for user in users[:50]:
            limit = "∞" if int(user["user_id"]) == OWNER_ID or int(user["file_limit"]) < 0 else str(user["file_limit"])
            used = project_count(user["user_id"])
            kb.add(types.InlineKeyboardButton(
                f"👤 {user['user_id']} • {used}/{limit}",
                callback_data=f"user|{user['user_id']}",
            ))
        kb.add(types.InlineKeyboardButton("⬅️ Admin", callback_data="menu_admin"))
        bot.send_message(call.message.chat.id, "👥 *Users & Limits*\n\nSelect a user:", reply_markup=kb)
        return

    if data == "adm_files":
        rows = []
        for user in all_users():
            for project in get_projects(user["user_id"]):
                rows.append((int(user["user_id"]), project))
        if not rows:
            bot.send_message(call.message.chat.id, "No hosted projects.", reply_markup=admin_keyboard())
            return
        for target, project in rows[:100]:
            bot.send_message(
                call.message.chat.id,
                f"👤 `{target}`\n📄 *{project['name']}*\n{status_text(project)}",
                reply_markup=project_keyboard(target, project["name"], True),
            )
        return

    if data == "adm_broadcast":
        sent = bot.send_message(
            call.message.chat.id,
            "📢 *Send the broadcast text now.*\n\nPressing another bot button does not send a broadcast; send the text here.",
            reply_markup=admin_keyboard(),
        )
        bot.register_next_step_handler(sent, broadcast_logic)
        answer(call)
        return

    if data == "adm_video":
        sent = bot.send_message(
            call.message.chat.id,
            "🎥 *Send a video now* to save it as the welcome video.\n\nOr type `REMOVE` to clear it.",
            reply_markup=admin_keyboard(),
        )
        bot.register_next_step_handler(sent, save_video_logic)
        answer(call)
        return


# ----------------------- Callback router ------------------------
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    try:
        data = call.data or ""
        uid = int(call.from_user.id)
        ensure_user(call.from_user)

        # Main menu
        if data == "home":
            edit_or_send(call.message, welcome_text(call.from_user), main_keyboard(uid))
            answer(call)
            return

        if data == "menu_deploy":
            answer(call, "📤 Upload step opened")
            start_deployment(call.message, edit=True)
            return

        if data == "menu_files":
            answer(call, "📂 Opening projects...")
            show_my_files(call.message, edit=True)
            return

        if data == "menu_stats":
            answer(call, "📊 Loading...")
            send_statistics(call.message)
            return

        if data == "menu_info":
            answer(call)
            edit_or_send(
                call.message,
                "📚 *Hosting Info*\n\n"
                "• Python: `.py`\n"
                "• JavaScript: `.js`\n"
                "• Project: `.zip`\n"
                "• Default normal-user limit: *2 projects*\n"
                "• Owner: *Unlimited*\n"
                "• No points system\n"
                "• No referral system\n\n"
                "⚠️ Uploaded code runs in the same Railway container/service. Use trusted code.",
                home_keyboard(),
            )
            return

        if data == "menu_updates":
            answer(call)
            updates(call.message, edit=True)
            return

        if data == "menu_contact":
            answer(call)
            contact_owner(call.message, edit=True)
            return

        if data == "menu_admin":
            if uid != OWNER_ID:
                answer(call, "Owner only", True)
                return
            answer(call)
            edit_or_send(call.message, "👑 *Admin Control Center*", admin_keyboard())
            return

        # User-limit administration
        if data.startswith(("user|", "limadd|", "limsub|", "limset|", "limunlimited|", "limdefault|")):
            if uid != OWNER_ID:
                answer(call, "Owner only", True)
                return
            action, raw = data.split("|", 1)
            try:
                target = int(raw)
            except ValueError:
                answer(call, "Invalid user", True)
                return

            if action == "user":
                handle_user_admin(call, target)
                answer(call)
                return

            if target == OWNER_ID:
                answer(call, "Owner is always unlimited", True)
                return

            current = user_limit(target)
            if action == "limadd":
                new_limit = min(MAX_CONFIGURABLE_LIMIT, max(0, current) + 1) if current >= 0 else MAX_CONFIGURABLE_LIMIT
            elif action == "limsub":
                new_limit = max(0, current - 1) if current >= 0 else DEFAULT_USER_LIMIT
            elif action == "limunlimited":
                new_limit = -1
            elif action == "limdefault":
                new_limit = DEFAULT_USER_LIMIT
            else:
                sent = bot.send_message(
                    call.message.chat.id,
                    f"🔢 Send new project limit for `{target}`\n\nEnter `0-{MAX_CONFIGURABLE_LIMIT}` or `unlimited`.",
                    reply_markup=admin_keyboard(),
                )
                bot.register_next_step_handler(sent, set_limit_logic, target)
                answer(call)
                return

            set_user_limit(target, new_limit)
            answer(call, "Limit updated")
            handle_user_admin(call, target)
            return

        # Admin actions
        if data.startswith("adm_"):
            if uid != OWNER_ID:
                answer(call, "Owner only", True)
                return
            handle_admin_callback(call, data)
            return

        # Project actions
        parts = data.split("|", 2)
        if len(parts) == 3 and parts[0] in {"run", "stop", "down", "del", "log"}:
            action, target_raw, name = parts
            try:
                target = int(target_raw)
            except ValueError:
                answer(call, "Invalid user", True)
                return
            if target != uid and uid != OWNER_ID:
                answer(call, "Not authorized", True)
                return
            project = get_project(target, name)
            if not project:
                answer(call, "Project not found", True)
                return
            project_action(call, action, project)
            return

        answer(call)

    except Exception as exc:
        logging.exception("Callback error")
        # Never let a callback exception leave Telegram waiting forever.
        answer(call, f"Error: {str(exc)[:150]}", True)


# ----------------------- Recovery / shutdown --------------------
def restore_projects():
    """Try to restart saved projects after a Railway service restart."""
    for user in all_users():
        for project in get_projects(user["user_id"]):
            try:
                ok, detail = run_project(project)
                logging.info(
                    "restore user=%s project=%s ok=%s detail=%s",
                    user["user_id"], project["name"], ok, detail[:180].replace("\n", " "),
                )
            except Exception:
                logging.exception("restore failed for %s/%s", user["user_id"], project["name"])


def cleanup():
    for key in list(running_processes.keys()):
        try:
            stop_process(key)
        except Exception:
            logging.exception("cleanup failed")


# ---------------------------- Run -------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    init_db()
    restore_projects()
    logging.info("Premium Hosting Bot online")
    try:
        bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
    finally:
        cleanup()
