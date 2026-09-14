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
import zipfile
from pathlib import Path
from threading import Lock

import psutil
import telebot
from telebot import types

BASE_DIR = Path(__file__).resolve().parent
# On Railway, set DATA_ROOT=/data and attach a Railway Volume at /data.
DATA_ROOT = Path(os.getenv("DATA_ROOT", str(BASE_DIR / "data"))).resolve()
DATA_ROOT.mkdir(parents=True, exist_ok=True)
DEPLOY_DIR = DATA_ROOT / "deployed_bots"
LOG_DIR = DATA_ROOT / "logs"
DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_ROOT / "hosting.db"

MAX_UPLOAD_MB = max(1, int(os.getenv("MAX_UPLOAD_MB", "50")))
MAX_ZIP_EXTRACT_MB = max(1, int(os.getenv("MAX_ZIP_EXTRACT_MB", "200")))
MAX_ZIP_FILES = max(10, int(os.getenv("MAX_ZIP_FILES", "1000")))
DEFAULT_USER_LIMIT = max(1, int(os.getenv("DEFAULT_USER_LIMIT", "2")))
MAX_CONFIGURABLE_LIMIT = max(DEFAULT_USER_LIMIT, int(os.getenv("MAX_CONFIGURABLE_LIMIT", "100")))


def load_env_file():
    # Optional local .env/l.env compatibility; Railway should use Variables instead.
    candidates = [BASE_DIR / ".env", BASE_DIR / "l.env"]
    values = {}
    for path in candidates:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values


ENV = load_env_file()
API_TOKEN = os.getenv("BOT_TOKEN") or ENV.get("BOT_TOKEN")
OWNER_RAW = os.getenv("OWNER_USER_ID") or ENV.get("OWNER_USER_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID") or ENV.get("CHANNEL_ID", "")
OWNER_CONTACT = os.getenv("OWNER_CONTACT", "")

if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it in Railway Variables.")
try:
    OWNER_ID = int(OWNER_RAW or "0")
except ValueError:
    raise RuntimeError("OWNER_USER_ID must be a numeric Telegram user ID.")
if not OWNER_ID:
    raise RuntimeError("OWNER_USER_ID is missing. Add it in Railway Variables.")

bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")
DB_LOCK = Lock()
running_processes = {}
process_meta = {}

DEFAULT_SETTINGS = {
    "maintenance": 0,
    "welcome_video": "",
    "default_limit": DEFAULT_USER_LIMIT,
}


def db_connect():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
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
        for k, v in DEFAULT_SETTINGS.items():
            db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, str(v)))
        db.commit()


def get_setting(key, default=None):
    with DB_LOCK, db_connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with DB_LOCK, db_connect() as db:
        db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        db.commit()


def ensure_user(user):
    uid = int(user.id)
    now = int(time.time())
    default_limit = int(get_setting("default_limit", DEFAULT_USER_LIMIT))
    with DB_LOCK, db_connect() as db:
        row = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            limit = -1 if uid == OWNER_ID else default_limit
            db.execute("INSERT INTO users(user_id,first_name,username,file_limit,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                       (uid, user.first_name or "", user.username or "", limit, now, now))
        else:
            db.execute("UPDATE users SET first_name=?, username=?, updated_at=? WHERE user_id=?",
                       (user.first_name or "", user.username or "", now, uid))
        db.commit()


def user_limit(uid):
    if uid == OWNER_ID:
        return -1
    with DB_LOCK, db_connect() as db:
        row = db.execute("SELECT file_limit FROM users WHERE user_id=?", (uid,)).fetchone()
    return int(row["file_limit"]) if row else DEFAULT_USER_LIMIT


def set_user_limit(uid, limit):
    ensure_user(types.SimpleNamespace(id=uid, first_name="", username=""))
    with DB_LOCK, db_connect() as db:
        db.execute("UPDATE users SET file_limit=?, updated_at=? WHERE user_id=?", (limit, int(time.time()), uid))
        db.commit()


def project_count(uid):
    with DB_LOCK, db_connect() as db:
        return int(db.execute("SELECT COUNT(*) FROM projects WHERE user_id=?", (uid,)).fetchone()[0])


def get_projects(uid):
    with DB_LOCK, db_connect() as db:
        return db.execute("SELECT * FROM projects WHERE user_id=? ORDER BY id DESC", (uid,)).fetchall()


def get_project(uid, name):
    with DB_LOCK, db_connect() as db:
        return db.execute("SELECT * FROM projects WHERE user_id=? AND name=?", (uid, name)).fetchone()


def add_project(uid, name, root, entry, kind):
    with DB_LOCK, db_connect() as db:
        db.execute("INSERT INTO projects(user_id,name,root,entry,kind,created_at) VALUES(?,?,?,?,?,?)",
                   (uid, name, root, entry, kind, int(time.time())))
        db.commit()


def delete_project_record(uid, name):
    with DB_LOCK, db_connect() as db:
        db.execute("DELETE FROM projects WHERE user_id=? AND name=?", (uid, name))
        db.commit()


def all_users():
    with DB_LOCK, db_connect() as db:
        return db.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()


def safe_name(name: str) -> str:
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip(".")
    if not name or name in {".", ".."}:
        raise ValueError("Invalid file name")
    return name[:180]


def user_root(uid):
    p = DEPLOY_DIR / str(uid)
    p.mkdir(parents=True, exist_ok=True)
    return p


def is_within(child: Path, parent: Path):
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


def find_entrypoint(root):
    package = root / "package.json"
    if package.exists():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
            start = data.get("scripts", {}).get("start")
            if start:
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
    "telebot": "pyTelegramBotAPI", "telegram": "python-telegram-bot", "requests": "requests",
    "aiohttp": "aiohttp", "httpx": "httpx", "bs4": "beautifulsoup4", "cv2": "opencv-python-headless",
    "PIL": "Pillow", "dotenv": "python-dotenv", "yaml": "PyYAML", "dateutil": "python-dateutil",
    "pytz": "pytz", "flask": "Flask", "fastapi": "fastapi", "uvicorn": "uvicorn", "psutil": "psutil",
}
PY_STDLIB = set("abc argparse asyncio base64 collections csv datetime decimal email enum functools hashlib html http inspect io itertools json logging math multiprocessing os pathlib pickle platform random re secrets shutil signal socket sqlite3 statistics string subprocess sys tempfile textwrap threading time traceback typing unittest urllib uuid warnings weakref xml zipfile zlib".split())


def detect_imports(root):
    found = set()
    for p in root.rglob("*.py"):
        if ".venv" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in re.finditer(r"^\s*(?:from\s+([A-Za-z_][\w]*)|import\s+([A-Za-z_][\w]*))", text, re.MULTILINE):
            found.add(m.group(1) or m.group(2))
    return sorted(found)


def install_python_dependencies(root):
    req = root / "requirements.txt"
    venv = root / ".venv"
    if not venv.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    py = venv / "bin" / "python"
    if not py.exists():
        py = venv / "Scripts" / "python.exe"
    if not py.exists():
        raise RuntimeError("Python virtual environment could not be created")
    subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=180)
    if req.exists():
        result = subprocess.run([str(py), "-m", "pip", "install", "-r", str(req)], capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-5000:] or "pip install failed")
    else:
        local_names = {p.stem for p in root.rglob("*.py")}
        packages = []
        for mod in detect_imports(root):
            if mod in PY_STDLIB or mod in local_names or mod.startswith("_"):
                continue
            pkg = IMPORT_MAP.get(mod)
            if pkg and pkg not in packages:
                packages.append(pkg)
        if packages:
            result = subprocess.run([str(py), "-m", "pip", "install", *packages], capture_output=True, text=True, timeout=600)
            if result.returncode != 0:
                raise RuntimeError(result.stderr[-5000:] or "dependency install failed")
    return str(py)


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
        process_meta.pop(key, None)
    return True


def is_running(project):
    root = DEPLOY_DIR / project["root"]
    entry = root / project["entry"]
    key = str(entry.resolve())
    proc = running_processes.get(key)
    return bool(proc and proc.poll() is None)


def run_project(project):
    uid = int(project["user_id"])
    root = DEPLOY_DIR / project["root"]
    entry = root / project["entry"]
    kind = project["kind"]
    if not root.exists() or not entry.exists():
        return False, "Project files are missing"
    key = str(entry.resolve())
    if key in running_processes and running_processes[key].poll() is None:
        return True, "Already running"

    app_root = root / "app" if (root / "app").exists() else root
    if kind == "python":
        interpreter = install_python_dependencies(app_root)
        cmd = [interpreter, str(entry)]
    elif kind == "node":
        if shutil.which("node") is None:
            return False, "Node.js is not installed on the hosting service"
        if (app_root / "package.json").exists() and not (app_root / "node_modules").exists():
            result = subprocess.run(["npm", "install", "--omit=dev"], cwd=str(app_root), capture_output=True, text=True, timeout=600)
            if result.returncode != 0:
                return False, result.stderr[-5000:] or "npm install failed"
        cmd = ["node", str(entry)]
    elif kind == "node-package":
        if shutil.which("npm") is None:
            return False, "npm is not installed on the hosting service"
        result = subprocess.run(["npm", "install", "--omit=dev"], cwd=str(app_root), capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            return False, result.stderr[-5000:] or "npm install failed"
        cmd = ["npm", "start", "--if-present"]
    else:
        return False, "Unsupported project type"

    log_path = LOG_DIR / f"{uid}_{safe_name(project['name'])}.log"
    log_file = log_path.open("a", encoding="utf-8")
    log_file.write(f"\n--- START {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    log_file.flush()
    # Never expose the hosting bot's private credentials/platform secrets to uploaded code.
    blocked_env = {
        "BOT_TOKEN", "OWNER_USER_ID", "CHANNEL_ID", "OWNER_CONTACT",
        "RAILWAY_TOKEN", "RAILWAY_API_TOKEN", "DATABASE_URL",
    }
    env = {k: v for k, v in os.environ.items() if k not in blocked_env and not k.upper().endswith("_TOKEN")}
    env["HOSTING_USER_ID"] = str(uid)
    kwargs = dict(cwd=str(app_root), stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT, text=True, env=env)
    if os.name != "nt":
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception as exc:
        log_file.close()
        return False, str(exc)
    time.sleep(3)
    if proc.poll() is not None:
        log_file.close()
        try:
            detail = log_path.read_text(encoding="utf-8", errors="ignore")[-5000:]
        except OSError:
            detail = "Process exited immediately"
        return False, detail or "Process exited immediately"
    running_processes[key] = proc
    process_meta[key] = {"user_id": uid, "project": project["name"], "log": str(log_path), "started": int(time.time())}
    return True, "Running"


def status_text(project):
    return "🟢 RUNNING" if is_running(project) else "🔴 STOPPED"


def main_keyboard(uid):
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(types.InlineKeyboardButton("📤 Deploy File", callback_data="menu_deploy"),
          types.InlineKeyboardButton("📂 My Files", callback_data="menu_files"))
    m.add(types.InlineKeyboardButton("📊 Statistics", callback_data="menu_stats"),
          types.InlineKeyboardButton("ℹ️ Hosting Info", callback_data="menu_info"))
    m.add(types.InlineKeyboardButton("📢 Updates", callback_data="menu_updates"),
          types.InlineKeyboardButton("📞 Contact Owner", callback_data="menu_contact"))
    if uid == OWNER_ID:
        m.add(types.InlineKeyboardButton("👑 Admin Panel", callback_data="menu_admin"))
    return m


def admin_keyboard():
    maint = "🟢 Maintenance OFF" if get_setting("maintenance", "0") != "1" else "🔴 Maintenance ON"
    return types.InlineKeyboardMarkup(row_width=2).add(
        types.InlineKeyboardButton("👥 Users & Limits", callback_data="adm_users"),
        types.InlineKeyboardButton("🌍 All Files", callback_data="adm_files"),
        types.InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast"),
        types.InlineKeyboardButton("🎥 Welcome Video", callback_data="adm_video"),
        types.InlineKeyboardButton(maint, callback_data="adm_maintenance"),
        types.InlineKeyboardButton("🖥 Server Stats", callback_data="adm_stats"),
    )


def back_home_markup():
    return types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🏠 Home", callback_data="home"))


def project_keyboard(uid, name, owner_view=False):
    safe_uid = str(uid)
    return types.InlineKeyboardMarkup(row_width=2).add(
        types.InlineKeyboardButton("▶️ RUN", callback_data=f"run|{safe_uid}|{name}"),
        types.InlineKeyboardButton("⏹ STOP", callback_data=f"stop|{safe_uid}|{name}"),
        types.InlineKeyboardButton("📜 LOG", callback_data=f"log|{safe_uid}|{name}"),
        types.InlineKeyboardButton("📥 DOWNLOAD", callback_data=f"down|{safe_uid}|{name}"),
        types.InlineKeyboardButton("🗑 DELETE", callback_data=f"del|{safe_uid}|{name}"),
    )


def welcome_text(message):
    uid = message.from_user.id
    limit = "∞ Unlimited" if uid == OWNER_ID or user_limit(uid) < 0 else str(user_limit(uid))
    used = project_count(uid)
    maint = "🔴 Maintenance" if get_setting("maintenance", "0") == "1" else "🟢 Online"
    return (
        "┏━━━━━━━━━━━━━━━━━━━━━━┓\n"
        "┃ ⚡ *PREMIUM HOSTING* ⚡ ┃\n"
        "┗━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
        f"👋 Welcome, *{message.from_user.first_name or 'User'}*!\n\n"
        "🚀 Python / JavaScript / ZIP hosting\n"
        "📦 Automatic dependency setup\n"
        "📜 Project logs & process control\n"
        "🛡️ Upload & ZIP safety checks\n\n"
        f"👤 ID: `{uid}`\n"
        f"📂 Projects: *{used}/{limit}*\n"
        f"⚡ Status: *{maint}*\n\n"
        "👇 Choose an option below:"
    )


@bot.message_handler(commands=["start"])
def start(message):
    ensure_user(message.from_user)
    if get_setting("maintenance", "0") == "1" and message.from_user.id != OWNER_ID:
        bot.send_message(message.chat.id, "🔴 *Hosting is temporarily under maintenance.*")
        return
    text = welcome_text(message)
    video_id = get_setting("welcome_video", "")
    if video_id:
        try:
            bot.send_video(message.chat.id, video_id, caption=text, reply_markup=main_keyboard(message.from_user.id))
            return
        except Exception:
            pass
    bot.send_message(message.chat.id, text, reply_markup=main_keyboard(message.from_user.id))


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    try:
        data = call.data or ""
        uid = call.from_user.id
        ensure_user(call.from_user)
        if data == "home":
            bot.edit_message_text(welcome_text(call.message), call.message.chat.id, call.message.message_id, reply_markup=main_keyboard(uid))
            bot.answer_callback_query(call.id)
            return
        if data == "menu_deploy":
            start_deployment(call.message, edit=True)
            bot.answer_callback_query(call.id)
            return
        if data == "menu_files":
            show_my_files(call.message, edit=True)
            bot.answer_callback_query(call.id)
            return
        if data == "menu_stats":
            send_statistics(call.message)
            bot.answer_callback_query(call.id)
            return
        if data == "menu_info":
            bot.edit_message_text(
                "📚 *Hosting Info*\n\n• Python: `.py`\n• JavaScript: `.js`\n• Project: `.zip`\n• Default user limit: *2 projects*\n• Owner: *Unlimited*\n• No points system\n• No referral system\n\n⚠️ Uploaded code runs with the same OS account as this service. Use only trusted code.",
                call.message.chat.id, call.message.message_id, reply_markup=back_home_markup())
            bot.answer_callback_query(call.id)
            return
        if data == "menu_updates":
            updates(call.message, edit=True)
            bot.answer_callback_query(call.id)
            return
        if data == "menu_contact":
            contact_owner(call.message, edit=True)
            bot.answer_callback_query(call.id)
            return
        if data == "menu_admin":
            if uid != OWNER_ID:
                bot.answer_callback_query(call.id, "Owner only", show_alert=True); return
            bot.edit_message_text("👑 *Admin Control Center*", call.message.chat.id, call.message.message_id, reply_markup=admin_keyboard())
            bot.answer_callback_query(call.id); return

        if data.startswith("user|") or data.startswith("limadd|") or data.startswith("limsub|") or data.startswith("limset|") or data.startswith("limunlimited|") or data.startswith("limdefault|"):
            if uid != OWNER_ID:
                bot.answer_callback_query(call.id, "Owner only", show_alert=True); return
            action, raw = data.split("|", 1)
            try:
                target = int(raw)
            except ValueError:
                bot.answer_callback_query(call.id, "Invalid user", show_alert=True); return
            if action == "user":
                handle_user_admin(call, target); bot.answer_callback_query(call.id); return
            if target == OWNER_ID:
                bot.answer_callback_query(call.id, "Owner is always unlimited", show_alert=True); return
            current = user_limit(target)
            if action == "limadd": new = min(MAX_CONFIGURABLE_LIMIT, max(0, current if current >= 0 else MAX_CONFIGURABLE_LIMIT) + 1)
            elif action == "limsub": new = max(0, current - 1) if current >= 0 else DEFAULT_USER_LIMIT
            elif action == "limunlimited": new = -1
            elif action == "limdefault": new = DEFAULT_USER_LIMIT
            else:
                sent = bot.send_message(call.message.chat.id, f"🔢 Send new project limit for `{target}` (0-{MAX_CONFIGURABLE_LIMIT}, or `unlimited`):")
                bot.register_next_step_handler(sent, set_limit_logic, target)
                bot.answer_callback_query(call.id); return
            set_user_limit(target, new)
            bot.answer_callback_query(call.id, "Limit updated")
            handle_user_admin(call, target)
            return

        if data.startswith("adm_"):
            if uid != OWNER_ID:
                bot.answer_callback_query(call.id, "Owner only", show_alert=True); return
            handle_admin_callback(call, data)
            return

        parts = data.split("|", 2)
        if len(parts) == 3 and parts[0] in {"run", "stop", "down", "del", "log"}:
            action, target_raw, name = parts
            try: target = int(target_raw)
            except ValueError:
                bot.answer_callback_query(call.id, "Invalid user", show_alert=True); return
            if target != uid and uid != OWNER_ID:
                bot.answer_callback_query(call.id, "Not authorized", show_alert=True); return
            project = get_project(target, name)
            if not project:
                bot.answer_callback_query(call.id, "Project not found", show_alert=True); return
            handle_project_action(call, action, project)
            return
        bot.answer_callback_query(call.id)
    except Exception as exc:
        logging.exception("Callback error")
        try: bot.answer_callback_query(call.id, f"Error: {str(exc)[:120]}", show_alert=True)
        except Exception: pass


def handle_project_action(call, action, project):
    uid = int(project["user_id"])
    name = project["name"]
    if action == "run":
        ok, detail = run_project(project)
        bot.answer_callback_query(call.id, "🟢 Running" if ok else f"❌ {detail[:150]}", show_alert=not ok)
    elif action == "stop":
        root = DEPLOY_DIR / project["root"]
        key = str((root / project["entry"]).resolve())
        bot.answer_callback_query(call.id, "⏹ Stopped" if stop_process(key) else "Already stopped")
    elif action == "log":
        root = DEPLOY_DIR / project["root"]
        log = LOG_DIR / f"{uid}_{safe_name(name)}.log"
        text = log.read_text(encoding="utf-8", errors="ignore")[-3500:] if log.exists() else "No logs yet."
        bot.send_message(call.message.chat.id, f"📜 *{name} logs*\n\n```\n{text}\n```", reply_markup=back_home_markup())
        bot.answer_callback_query(call.id)
    elif action == "down":
        root = DEPLOY_DIR / project["root"]
        entry = root / project["entry"]
        if entry.exists() and entry.is_file():
            with entry.open("rb") as f:
                bot.send_document(call.message.chat.id, f, caption=f"📥 `{name}`")
            bot.answer_callback_query(call.id, "Sent")
        else:
            bot.answer_callback_query(call.id, "File not found", show_alert=True)
    elif action == "del":
        root = DEPLOY_DIR / project["root"]
        key = str((root / project["entry"]).resolve())
        stop_process(key)
        shutil.rmtree(root, ignore_errors=True)
        delete_project_record(uid, name)
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.answer_callback_query(call.id, "🗑 Deleted")


def start_deployment(message, edit=False):
    uid = message.chat.id
    limit = user_limit(uid)
    used = project_count(uid)
    if limit >= 0 and used >= limit:
        text = f"🚫 *Hosting limit reached.*\n\nUsed: `{used}/{limit}`\n\nAsk the Owner to increase your limit if needed."
        if edit:
            bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=back_home_markup())
        else:
            bot.send_message(message.chat.id, text, reply_markup=back_home_markup())
        return
    text = "📤 *Upload your project now*\n\nSend one `.py`, `.js`, or `.zip` file as a Telegram document.\n\n⚠️ Only upload code you trust."
    if edit:
        bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=back_home_markup())
        sent = bot.send_message(message.chat.id, "📎 *Waiting for your file...*")
    else:
        sent = bot.send_message(message.chat.id, text, reply_markup=back_home_markup())
    bot.register_next_step_handler(sent, process_upload)


def process_upload(message):
    if not message.document:
        bot.send_message(message.chat.id, "❌ Please send the project as a document.", reply_markup=back_home_markup()); return
    uid = message.from_user.id
    ensure_user(message.from_user)
    limit = user_limit(uid)
    if limit >= 0 and project_count(uid) >= limit:
        bot.send_message(message.chat.id, f"🚫 Your limit is {limit} projects.", reply_markup=back_home_markup()); return
    original = safe_name(message.document.file_name or "upload")
    ext = Path(original).suffix.lower()
    if ext not in {".py", ".js", ".zip"}:
        bot.send_message(message.chat.id, "❌ Supported formats: `.py`, `.js`, `.zip`", reply_markup=back_home_markup()); return
    file_size = int(getattr(message.document, "file_size", 0) or 0)
    if file_size and file_size > MAX_UPLOAD_MB * 1024 * 1024:
        bot.send_message(message.chat.id, f"❌ File is too large. Maximum: {MAX_UPLOAD_MB} MB.", reply_markup=back_home_markup()); return

    base = safe_name(Path(original).stem) or "project"
    bot_dir = user_root(uid) / f"{base}_{int(time.time())}"
    bot_dir.mkdir(parents=True, exist_ok=False)
    prog = bot.send_message(message.chat.id, "⏳ *Preparing deployment...*")
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
                if bad: raise ValueError(f"Corrupt ZIP entry: {bad}")
                safe_extract(zf, bot_dir / "app")
            app_root = bot_dir / "app"
            entry, kind = find_entrypoint(app_root)
            if not entry:
                raise ValueError("ZIP contains no supported Python/Node entrypoint")
            if kind == "node-package":
                rel_entry = "app/package.json"
            else:
                rel_entry = str(entry.relative_to(bot_dir))
        else:
            entry = bot_dir / original
            entry.write_bytes(content)
            kind = "python" if ext == ".py" else "node"
            rel_entry = original
        name = original
        if get_project(uid, name):
            raise ValueError("A project with this file name already exists")
        temp_project = {"user_id": uid, "name": name, "root": str(bot_dir.relative_to(DEPLOY_DIR)), "entry": rel_entry, "kind": kind}
        ok, detail = run_project(temp_project)
        if not ok:
            raise RuntimeError(detail)
        add_project(uid, name, temp_project["root"], rel_entry, kind)
        used = project_count(uid); lim = user_limit(uid)
        limit_text = "∞" if lim < 0 else str(lim)
        bot.edit_message_text(f"🚀 *Deployment successful!*\n\n📄 `{name}`\n🟢 Status: RUNNING\n📂 Projects: `{used}/{limit_text}`", message.chat.id, prog.message_id, reply_markup=back_home_markup())
    except Exception as exc:
        shutil.rmtree(bot_dir, ignore_errors=True)
        bot.edit_message_text(f"❌ *Deployment failed.*\n\n`{str(exc)[-3500:]}`", message.chat.id, prog.message_id, reply_markup=back_home_markup())


def show_my_files(message, edit=False):
    rows = get_projects(message.from_user.id)
    text = "📂 *My Projects*\n\n" if rows else "📂 *My Projects*\n\nNo projects yet."
    limit = user_limit(message.from_user.id)
    limit_text = "∞" if limit < 0 else str(limit)
    text += f"Usage: `{len(rows)}/{limit_text}`"
    if edit:
        bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=back_home_markup())
    else:
        bot.send_message(message.chat.id, text, reply_markup=back_home_markup())
    for p in rows:
        bot.send_message(message.chat.id, f"📄 *{p['name']}*\n{status_text(p)}\nType: `{p['kind']}`", reply_markup=project_keyboard(p["user_id"], p["name"]))


def send_statistics(message):
    users = all_users()
    total = 0; running = 0
    for u in users:
        ps = get_projects(u["user_id"]); total += len(ps); running += sum(1 for p in ps if is_running(p))
    bot.send_message(message.chat.id, f"📊 *Hosting Statistics*\n\n👥 Users: `{len(users)}`\n📦 Projects: `{total}`\n🟢 Running: `{running}`\n🔴 Stopped: `{total-running}`", reply_markup=back_home_markup())


def updates(message, edit=False):
    if not CHANNEL_ID:
        text = "📢 Updates channel is not configured."
        markup = back_home_markup()
    else:
        username = CHANNEL_ID.lstrip("@")
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("📢 JOIN CHANNEL", url=f"https://t.me/{username}"), types.InlineKeyboardButton("🏠 Home", callback_data="home"))
        text = "📢 *Stay updated*\n\nJoin our official updates channel."
    if edit: bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=markup)
    else: bot.send_message(message.chat.id, text, reply_markup=markup)


def contact_owner(message, edit=False):
    text = f"📞 *Contact Owner*\n\n{OWNER_CONTACT or 'Owner contact is not configured.'}"
    if edit: bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=back_home_markup())
    else: bot.send_message(message.chat.id, text, reply_markup=back_home_markup())


def handle_admin_callback(call, data):
    if data == "adm_maintenance":
        new = "0" if get_setting("maintenance", "0") == "1" else "1"
        set_setting("maintenance", new)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=admin_keyboard())
        bot.answer_callback_query(call.id, "Updated")
    elif data == "adm_stats":
        cpu = psutil.cpu_percent(interval=0.2); ram = psutil.virtual_memory().percent; disk = psutil.disk_usage(str(DATA_ROOT)).percent
        bot.answer_callback_query(call.id, f"CPU {cpu}% | RAM {ram}% | Disk {disk}%", show_alert=True)
    elif data == "adm_users":
        users = all_users()
        if not users:
            bot.send_message(call.message.chat.id, "No users yet.", reply_markup=back_home_markup()); return
        text = "👥 *Users & Limits*\n\nSelect a user:"
        markup = types.InlineKeyboardMarkup(row_width=1)
        for u in users[:50]:
            limit = "∞" if int(u["file_limit"]) < 0 or u["user_id"] == OWNER_ID else str(u["file_limit"])
            markup.add(types.InlineKeyboardButton(f"👤 {u['user_id']} • {len(get_projects(u['user_id']))}/{limit}", callback_data=f"user|{u['user_id']}"))
        markup.add(types.InlineKeyboardButton("🏠 Admin", callback_data="menu_admin"))
        bot.send_message(call.message.chat.id, text, reply_markup=markup)
    elif data == "adm_files":
        rows = []
        for u in all_users():
            for p in get_projects(u["user_id"]): rows.append((u["user_id"], p))
        if not rows:
            bot.send_message(call.message.chat.id, "No hosted projects.", reply_markup=back_home_markup()); return
        for target, p in rows[:100]:
            bot.send_message(call.message.chat.id, f"👤 `{target}`\n📄 *{p['name']}*\n{status_text(p)}", reply_markup=project_keyboard(target, p["name"], True))
    elif data == "adm_broadcast":
        sent = bot.send_message(call.message.chat.id, "📢 Send the broadcast text now:", reply_markup=back_home_markup())
        bot.register_next_step_handler(sent, broadcast_logic)
    elif data == "adm_video":
        sent = bot.send_message(call.message.chat.id, "🎥 Send a video to save as the welcome video. Send `REMOVE` to clear it.")
        bot.register_next_step_handler(sent, save_video_logic)


def handle_user_admin(call, target):
    if target == OWNER_ID:
        limit = "∞"
    else:
        limit = "∞" if user_limit(target) < 0 else str(user_limit(target))
    used = project_count(target)
    markup = types.InlineKeyboardMarkup(row_width=2).add(
        types.InlineKeyboardButton("➕ Limit +1", callback_data=f"limadd|{target}"),
        types.InlineKeyboardButton("➖ Limit -1", callback_data=f"limsub|{target}"),
        types.InlineKeyboardButton("🔢 Set Limit", callback_data=f"limset|{target}"),
        types.InlineKeyboardButton("♾️ Unlimited", callback_data=f"limunlimited|{target}"),
        types.InlineKeyboardButton("🔄 Default (2)", callback_data=f"limdefault|{target}"),
        types.InlineKeyboardButton("⬅️ Users", callback_data="adm_users"),
    )
    bot.send_message(call.message.chat.id, f"👤 User: `{target}`\n📂 Projects: `{used}/{limit}`", reply_markup=markup)


def broadcast_logic(message):
    if message.from_user.id != OWNER_ID: return
    text = message.text or ""
    sent = failed = 0
    for u in all_users():
        try: bot.send_message(u["user_id"], f"📢 *Announcement*\n\n{text}"); sent += 1
        except Exception: failed += 1
    bot.send_message(message.chat.id, f"✅ Sent: `{sent}`\n❌ Failed: `{failed}`", reply_markup=admin_keyboard())


def save_video_logic(message):
    if message.from_user.id != OWNER_ID: return
    if (message.text or "").strip().upper() == "REMOVE":
        set_setting("welcome_video", "")
        bot.send_message(message.chat.id, "✅ Welcome video removed.", reply_markup=admin_keyboard()); return
    if message.video:
        set_setting("welcome_video", message.video.file_id)
        bot.send_message(message.chat.id, "✅ Welcome video saved.", reply_markup=admin_keyboard())
    else:
        bot.send_message(message.chat.id, "❌ Please send a video or `REMOVE`.", reply_markup=admin_keyboard())



def set_limit_logic(message, target):
    if message.from_user.id != OWNER_ID: return
    raw = (message.text or "").strip().lower()
    try:
        new = -1 if raw == "unlimited" else int(raw)
        if new < -1 or new > MAX_CONFIGURABLE_LIMIT: raise ValueError
        if target == OWNER_ID: new = -1
        set_user_limit(target, new)
        bot.send_message(message.chat.id, f"✅ User `{target}` limit set to `{'Unlimited' if new < 0 else new}`.", reply_markup=admin_keyboard())
    except Exception:
        bot.send_message(message.chat.id, f"❌ Enter 0-{MAX_CONFIGURABLE_LIMIT} or `unlimited`.", reply_markup=admin_keyboard())


def restore_projects():
    """Restart projects after the Railway service/container restarts."""
    for u in all_users():
        for p in get_projects(u["user_id"]):
            try:
                ok, detail = run_project(p)
                logging.info("restore %s/%s: %s %s", u["user_id"], p["name"], ok, detail[:120])
            except Exception:
                logging.exception("restore failed for %s/%s", u["user_id"], p["name"])


def cleanup():
    for key in list(running_processes):
        stop_process(key)


if __name__ == "__main__":
    init_db()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    restore_projects()
    print("Premium Hosting Bot online")
    try:
        bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
    finally:
        cleanup()
