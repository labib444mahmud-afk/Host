from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import psutil
import telebot
from telebot import types

# ============================================================
# LABIB HOSTING BOT - Railway ready
# ============================================================
# Configuration is read from Railway Variables first, then l.env
# for local testing. Do NOT commit real tokens to GitHub.

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DEPLOY_DIR = BASE_DIR / "deployed_bots"
LOG_DIR = BASE_DIR / "logs"
TMP_DIR = BASE_DIR / "tmp"
for folder in (DATA_DIR, DEPLOY_DIR, LOG_DIR, TMP_DIR):
    folder.mkdir(parents=True, exist_ok=True)

DB_FILE = DATA_DIR / "users_data.json"
SETTINGS_FILE = DATA_DIR / "bot_settings.json"
ENV_FILE = BASE_DIR / "l.env"
LOCK = threading.RLock()
USER_LOCKS: dict[int, threading.RLock] = {}


def load_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return result
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


ENV = load_env(ENV_FILE)
BOT_TOKEN = os.getenv("BOT_TOKEN") or ENV.get("BOT_TOKEN")
OWNER_RAW = os.getenv("OWNER_USER_ID") or ENV.get("OWNER_USER_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID") or ENV.get("CHANNEL_ID", "@Labib_channel")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it in Railway Variables or l.env.")
try:
    OWNER_ID = int(OWNER_RAW or "0")
except ValueError as exc:
    raise RuntimeError("OWNER_USER_ID must be a numeric Telegram user ID.") from exc
if not OWNER_ID:
    raise RuntimeError("OWNER_USER_ID is missing. Add it in Railway Variables or l.env.")


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


HOSTING_COST = env_int("HOSTING_COST", 4)
REFERRAL_POINTS = env_int("REFERRAL_POINTS", 2)
MAX_RUNNING_BOTS = env_int("MAX_RUNNING_BOTS", 30, 1)
MAX_UPLOAD_MB = env_int("MAX_UPLOAD_MB", 20, 1)
MAX_EXTRACTED_MB = env_int("MAX_EXTRACTED_MB", 100, 1)
MAX_ZIP_FILES = env_int("MAX_ZIP_FILES", 500, 1)
MAX_LOG_MB = env_int("MAX_LOG_MB", 5, 1)
DEPLOY_TIMEOUT = env_int("DEPLOY_TIMEOUT", 300, 30)
MEMORY_LIMIT_MB = env_int("BOT_MEMORY_LIMIT_MB", 256, 64)
CPU_LIMIT_PERCENT = env_int("BOT_CPU_LIMIT_PERCENT", 120, 0)
GLOBAL_MEMORY_RESERVE_PERCENT = env_int("GLOBAL_MEMORY_RESERVE_PERCENT", 15, 0)

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode=None,
    threaded=True,
    num_threads=20,
)

DEFAULT_SETTINGS = {
    "maintenance": False,
    "welcome_video": None,
    "hosting_cost": HOSTING_COST,
    "referral_points": REFERRAL_POINTS,
}


# ------------------------- JSON storage ----------------------

def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        with path.open("r", encoding="utf-8") as fh:
            value = json.load(fh)
        return value
    except (OSError, ValueError, TypeError):
        return json.loads(json.dumps(default))


def atomic_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


users_db: dict[str, Any] = load_json(DB_FILE, {})
settings: dict[str, Any] = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
for key, value in DEFAULT_SETTINGS.items():
    settings.setdefault(key, value)

# Runtime-only process table. Metadata is stored in users_db so Railway
# restarts can recreate the processes.
running: dict[str, subprocess.Popen] = {}
process_logs: dict[str, Any] = {}

# callback tokens keep Telegram callback_data comfortably below 64 bytes.
CALLBACKS: dict[str, tuple[str, int, str]] = {}
CALLBACKS_LOCK = threading.RLock()

# Deployments/installations run outside Telegram worker threads.
DEPLOY_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="deploy")
BROADCAST_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="broadcast")


def save_db() -> None:
    with LOCK:
        atomic_json(DB_FILE, users_db)


def save_settings() -> None:
    with LOCK:
        atomic_json(SETTINGS_FILE, settings)


def ensure_user(uid: int) -> dict[str, Any]:
    key = str(uid)
    with LOCK:
        if key not in users_db or not isinstance(users_db[key], dict):
            users_db[key] = {"username": "", "first_name": "", "points": 10, "files": {}}
        user = users_db[key]
        user.setdefault("username", "")
        user.setdefault("first_name", "")
        user.setdefault("points", 10)
        user.setdefault("files", {})
        if isinstance(user["files"], list):
            user["files"] = {str(x): {"legacy": True} for x in user["files"]}
        return user


def update_user_profile(message: Any) -> dict[str, Any]:
    user = ensure_user(message.from_user.id)
    username = getattr(message.from_user, "username", None) or ""
    first_name = getattr(message.from_user, "first_name", None) or ""
    changed = user.get("username") != username or user.get("first_name") != first_name
    user["username"] = username
    user["first_name"] = first_name
    if changed:
        save_db()
    return user


def get_user_lock(uid: int) -> threading.RLock:
    with LOCK:
        return USER_LOCKS.setdefault(uid, threading.RLock())


# ------------------------- formatting ------------------------
def clean_text(text: str, limit: int = 3500) -> str:
    text = (text or "").replace("\x00", "")
    return text[-limit:]


def welcome_text(message: Any) -> str:
    data = update_user_profile(message)
    name = data.get("first_name") or "User"
    hosted = len(data.get("files", {}))
    return (
        "╭───────────────────────╮\n"
        "│       🚀 LABIB 𝑯𝑶𝑺𝑻𝑰𝑵𝑮\n"
        "│              𝑩𝑶𝑻\n"
        "├───────────────────────┤\n"
        "│\n"
        f"│  👋 𝑾𝒆𝒍𝒄𝒐𝒎𝒆, {name}!\n"
        "│\n"
        "│  ⚡ 𝑯𝒐𝒔𝒕 𝒚𝒐𝒖𝒓 𝑻𝒆𝒍𝒆𝒈𝒓𝒂𝒎 𝑩𝒐𝒕𝒔\n"
        "│     𝒇𝒂𝒔𝒕 & 𝒆𝒂𝒔𝒊𝒍𝒚.\n"
        "│\n"
        "│  📦 𝑷𝒚𝒕𝒉𝒐𝒏 • .py • 𝒁𝑰𝑷\n"
        "│  ⚙️ 𝟐𝟒/𝟕 𝑩𝒐𝒕 𝑯𝒐𝒔𝒕𝒊𝒏𝒈\n"
        "│\n"
        "├───────────────────────┤\n"
        f"│  💰 𝑷𝒐𝒊𝒏𝒕𝒔 : {int(data.get('points', 0))}\n"
        f"│  🤖 𝑯𝒐𝒔𝒕𝒆𝒅 : {hosted}\n"
        "╰───────────────────────╯"
    )


def main_keyboard(uid: int) -> types.ReplyKeyboardMarkup:
    m = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    m.row(types.KeyboardButton("🚀 Upload File"), types.KeyboardButton("📂 My Files"))
    m.row(types.KeyboardButton("💰 My Points"), types.KeyboardButton("🔗 Referral"))
    m.row(types.KeyboardButton("📊 Statistics"), types.KeyboardButton("📢 Updates"))
    m.row(types.KeyboardButton("👑 Contact Owner"))
    if uid == OWNER_ID:
        m.row(types.KeyboardButton("⚙️ Admin Panel"))
    return m


def admin_keyboard() -> types.InlineKeyboardMarkup:
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("💰 Add Points", callback_data="A:add"),
        types.InlineKeyboardButton("📢 Broadcast", callback_data="A:broadcast"),
    )
    m.add(
        types.InlineKeyboardButton("👤 Users", callback_data="A:users"),
        types.InlineKeyboardButton("📊 Server Stats", callback_data="A:stats"),
    )
    m.add(types.InlineKeyboardButton("🎥 Set Welcome Video", callback_data="A:setvideo"))
    if settings.get("welcome_video"):
        m.add(types.InlineKeyboardButton("🗑 Remove Welcome Video", callback_data="A:delvideo"))
    state = "🔴 Maintenance: ON" if settings.get("maintenance") else "🟢 Maintenance: OFF"
    m.add(types.InlineKeyboardButton(state, callback_data="A:maintenance"))
    return m


# ------------------------- safe paths ------------------------
def safe_name(name: str) -> str:
    name = Path(name or "file").name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip(".")
    if not name or name in {".", ".."}:
        raise ValueError("Invalid filename")
    return name[:120]


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def zip_limits_ok(zf: zipfile.ZipFile) -> None:
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_FILES:
        raise ValueError(f"ZIP has too many files. Maximum is {MAX_ZIP_FILES}.")
    total = 0
    for info in infos:
        if info.file_size < 0:
            raise ValueError("Invalid ZIP entry")
        total += info.file_size
        if total > MAX_EXTRACTED_MB * 1024 * 1024:
            raise ValueError(f"ZIP extracted size exceeds {MAX_EXTRACTED_MB} MB.")
        p = Path(info.filename)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"Unsafe ZIP path: {info.filename}")
        if len(info.filename) > 300:
            raise ValueError("ZIP contains an excessively long path")


def safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    zip_limits_ok(zf)
    for info in zf.infolist():
        member = Path(info.filename)
        target = (dest / member).resolve()
        if not is_within(target, dest):
            raise ValueError(f"Unsafe ZIP path: {info.filename}")
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with zf.open(info, "r") as src, target.open("wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > info.file_size + 1:
                    raise ValueError("Invalid ZIP entry size")
                dst.write(chunk)


def find_entrypoint(root: Path) -> Path | None:
    preferred = ["bot.py", "main.py", "app.py", "run.py", "index.py", "index.js", "bot.js", "main.js"]
    for name in preferred:
        for p in sorted(root.rglob(name)):
            if p.is_file() and ".venv" not in p.parts and "node_modules" not in p.parts:
                return p
    py = sorted(p for p in root.rglob("*.py") if p.is_file() and ".venv" not in p.parts)
    if py:
        return py[0]
    js = sorted(p for p in root.rglob("*.js") if p.is_file() and "node_modules" not in p.parts)
    return js[0] if js else None


# ------------------------- dependency handling ---------------
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
    "google": "google-generativeai",
    "google.generativeai": "google-generativeai",
}
PY_STDLIB = {
    "abc", "argparse", "asyncio", "base64", "calendar", "collections", "contextlib", "copy", "csv",
    "datetime", "decimal", "email", "enum", "functools", "hashlib", "html", "http", "inspect", "io",
    "itertools", "json", "logging", "math", "multiprocessing", "os", "pathlib", "pickle", "platform",
    "random", "re", "secrets", "shutil", "signal", "socket", "sqlite3", "statistics", "string",
    "subprocess", "sys", "tempfile", "textwrap", "threading", "time", "traceback", "typing", "unittest",
    "urllib", "uuid", "warnings", "weakref", "xml", "zipfile", "zlib",
}


def detect_imports(root: Path) -> set[str]:
    found: set[str] = set()
    for p in root.rglob("*.py"):
        if ".venv" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in re.finditer(r"^\s*(?:from\s+([A-Za-z_][\w.]*)|import\s+([A-Za-z_][\w.]*))", text, re.MULTILINE):
            found.add((match.group(1) or match.group(2)).split(".")[0])
    return found


def command_timeout(cmd: list[str], cwd: Path, timeout: int = DEPLOY_TIMEOUT) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = clean_text(result.stdout or "", 3500)
        if result.returncode != 0:
            return False, output or f"Command failed with exit code {result.returncode}"
        return True, output
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout} seconds."
    except OSError as exc:
        return False, str(exc)


def install_python_dependencies(root: Path) -> Path:
    req = root / "requirements.txt"
    venv = root / ".venv"
    if not venv.exists():
        ok, detail = command_timeout([sys.executable, "-m", "venv", str(venv)])
        if not ok:
            raise RuntimeError(f"Virtualenv creation failed: {detail}")
    py = venv / "bin" / "python"
    if not py.exists():
        py = venv / "Scripts" / "python.exe"
    if not py.exists():
        raise RuntimeError("Could not create Python virtual environment")

    if req.exists():
        ok, detail = command_timeout([str(py), "-m", "pip", "install", "-r", str(req)], root)
        if not ok:
            raise RuntimeError(f"requirements.txt installation failed:\n{detail}")
    else:
        local_names = {p.stem for p in root.rglob("*.py")}
        packages: list[str] = []
        for module in sorted(detect_imports(root)):
            if module in PY_STDLIB or module in local_names or module.startswith("_"):
                continue
            package = IMPORT_MAP.get(module)
            if package and package not in packages:
                packages.append(package)
        if packages:
            ok, detail = command_timeout([str(py), "-m", "pip", "install", *packages], root)
            if not ok:
                raise RuntimeError(f"Automatic dependency installation failed:\n{detail}")
    return py


def node_available() -> bool:
    return shutil.which("node") is not None


# ------------------------- process manager --------------------
def process_key(uid: int, name: str) -> str:
    return f"{uid}:{name}"


def get_meta(uid: int, name: str) -> dict[str, Any] | None:
    return ensure_user(uid).get("files", {}).get(name)


def aggregate_running_memory() -> int:
    total = 0
    for proc in list(running.values()):
        if proc.poll() is not None:
            continue
        try:
            p = psutil.Process(proc.pid)
            total += p.memory_info().rss
            for child in p.children(recursive=True):
                try:
                    total += child.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return total


def resources_allow_new_process() -> tuple[bool, str]:
    active = sum(1 for p in running.values() if p.poll() is None)
    if active >= MAX_RUNNING_BOTS:
        return False, f"Hosting limit reached ({MAX_RUNNING_BOTS} running bots)."
    vm = psutil.virtual_memory()
    if vm.total > 0:
        reserve = GLOBAL_MEMORY_RESERVE_PERCENT
        if vm.available / vm.total * 100 < reserve:
            return False, "Server memory is currently too low. Please try again later."
    return True, "OK"


def terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    pass
            try:
                proc.kill()
            except OSError:
                pass
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def stop_bot(uid: int, name: str) -> bool:
    key = process_key(uid, name)
    proc = running.pop(key, None)
    process_logs.pop(key, None)
    if proc is None:
        return False
    terminate_process(proc)
    return True


def rotate_log(path: Path) -> None:
    limit = MAX_LOG_MB * 1024 * 1024
    try:
        if path.exists() and path.stat().st_size > limit:
            backup = path.with_suffix(path.suffix + ".1")
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass
            path.replace(backup)
    except OSError:
        pass


def start_bot(uid: int, name: str, automatic: bool = False) -> tuple[bool, str]:
    lock = get_user_lock(uid)
    with lock:
        meta = get_meta(uid, name)
        if not meta:
            return False, "File not found."
        key = process_key(uid, name)
        current = running.get(key)
        if current and current.poll() is None:
            return True, "Already running."
        allowed, reason = resources_allow_new_process()
        if not allowed and not automatic:
            return False, reason
        root = (DEPLOY_DIR / meta["root"]).resolve()

        if not root.exists() or not root.is_dir():
            return False, "Project directory is missing."

        entry = (root / meta["entry"]).resolve()

        if not is_within(root, entry):
            return False, "Invalid entrypoint path."

        if not entry.exists() or not entry.is_file():
            recovered = find_entrypoint(root)

            if recovered is None:
                return False, "Entrypoint file is missing. No .py or .js file was found."

            entry = recovered
            meta["entry"] = entry.relative_to(root).as_posix()
            save_db()
        ext = entry.suffix.lower()
        try:
            if ext == ".py":
                interpreter = install_python_dependencies(root)
                cmd = [str(interpreter), str(entry)]
            elif ext == ".js":
                if not node_available():
                    return False, "Node.js is not available on this Railway service."
                package_json = root / "package.json"
                if package_json.exists() and not (root / "node_modules").exists():
                    ok, detail = command_timeout(["npm", "install", "--omit=dev"], root)
                    if not ok:
                        return False, f"npm install failed:\n{detail}"
                cmd = ["node", str(entry)]
            else:
                return False, "Only .py, .js and ZIP projects are supported."
        except Exception as exc:
            return False, str(exc)

        log_path = LOG_DIR / f"{uid}_{meta.get('id', safe_name(name))}.log"
        rotate_log(log_path)
        try:
            log_file = log_path.open("a", encoding="utf-8")
        except OSError as exc:
            return False, f"Could not open log: {exc}"
        log_file.write(f"\n--- START {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        log_file.flush()

        # Only pass safe/common variables. The hosting bot token is deliberately
        # not inherited by user processes.
        child_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", str(root)),
            "PYTHONUNBUFFERED": "1",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        # Project-local l.env/.env is intentionally left for the hosted bot to load.
        kwargs: dict[str, Any] = {
            "cwd": str(root),
            "stdin": subprocess.DEVNULL,
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
            "env": child_env,
        }
        if os.name != "nt":
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except Exception as exc:
            log_file.close()
            return False, str(exc)
        running[key] = proc
        process_logs[key] = log_file
        meta["status"] = "running"
        meta["pid"] = proc.pid
        meta["last_started"] = int(time.time())
        save_db()
        return True, "Running"


def make_callback(action: str, uid: int, name: str) -> str:
    token = uuid.uuid4().hex[:10]
    with CALLBACKS_LOCK:
        CALLBACKS[token] = (action, uid, name)
        if len(CALLBACKS) > 5000:
            for old in list(CALLBACKS)[:1000]:
                CALLBACKS.pop(old, None)
    return f"F:{token}"


def status_of(uid: int, name: str) -> bool:
    key = process_key(uid, name)
    proc = running.get(key)
    return bool(proc and proc.poll() is None)


def file_keyboard(uid: int, name: str) -> types.InlineKeyboardMarkup:
    m = types.InlineKeyboardMarkup(row_width=2)
    if status_of(uid, name):
        m.add(types.InlineKeyboardButton("⏹ STOP", callback_data=make_callback("stop", uid, name)))
    else:
        m.add(types.InlineKeyboardButton("▶️ RUN", callback_data=make_callback("run", uid, name)))
    m.add(
        types.InlineKeyboardButton("🔄 RESTART", callback_data=make_callback("restart", uid, name)),
        types.InlineKeyboardButton("📥 DOWNLOAD", callback_data=make_callback("download", uid, name)),
    )
    m.add(types.InlineKeyboardButton("🗑 DELETE", callback_data=make_callback("delete", uid, name)))
    return m


# ------------------------- upload workflow --------------------
def deployment_job(chat_id: int, uid: int, original: str, content: bytes, progress_id: int) -> None:
    data = ensure_user(uid)
    cost = int(settings.get("hosting_cost", HOSTING_COST))
    lock = get_user_lock(uid)
    bot_dir: Path | None = None
    try:
        with lock:
            if int(data.get("points", 0)) < cost:
                bot.edit_message_text("❌ Insufficient points.", chat_id, progress_id)
                return
            stem = safe_name(Path(original).stem or "bot")
            bot_dir = user_root(uid) / f"{stem}_{uuid.uuid4().hex[:8]}"
            bot_dir.mkdir(parents=True, exist_ok=False)

        ext = Path(original).suffix.lower()
        if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
            raise ValueError(f"File is too large. Maximum upload size is {MAX_UPLOAD_MB} MB.")

        if ext == ".zip":
            archive = bot_dir / original
            archive.write_bytes(content)
            with zipfile.ZipFile(archive, "r") as zf:
                if zf.testzip() is not None:
                    raise ValueError("Corrupt ZIP file.")
                app_root = bot_dir / "app"
                safe_extract(zf, app_root)
            entry = find_entrypoint(app_root)
            if not entry:
                raise ValueError("ZIP contains no Python/JavaScript entrypoint.")
        else:
            entry = bot_dir / original
            entry.write_bytes(content)

        rel_entry = entry.relative_to(bot_dir).as_posix()
        record_id = uuid.uuid4().hex[:12]
        record = {
            "id": record_id,
            "root": str(bot_dir.relative_to(DEPLOY_DIR)),
            "entry": rel_entry,
            "original_name": original,
            "type": "zip" if ext == ".zip" else ext.lstrip("."),
            "archive": original if ext == ".zip" else None,
            "status": "starting",
            "created_at": int(time.time()),
            "auto_restart": True,
            "pid": None,
        }
        with lock:
            data["files"][original] = record
            save_db()

        ok, detail = start_bot(uid, original)
        if not ok:
            with lock:
                data["files"].pop(original, None)
                save_db()
            shutil.rmtree(bot_dir, ignore_errors=True)
            bot.edit_message_text(f"❌ Deployment failed.\n\n{clean_text(detail)}", chat_id, progress_id)
            return

        with lock:
            data["points"] = int(data.get("points", 0)) - cost
            data["files"][original]["status"] = "running"
            save_db()
        bot.edit_message_text(
            f"🚀 𝑩𝒐𝒕 𝑯𝒐𝒔𝒕𝒆𝒅 𝑺𝒖𝒄𝒄𝒆𝒔𝒔𝒇𝒖𝒍𝒍𝒚!\n\n"
            f"📄 {original}\n"
            f"🟢 Status: Running\n"
            f"💰 Remaining Points: {data['points']}",
            chat_id,
            progress_id,
        )
    except Exception as exc:
        if bot_dir:
            shutil.rmtree(bot_dir, ignore_errors=True)
        bot.edit_message_text(f"❌ Deployment error.\n\n{clean_text(str(exc))}", chat_id, progress_id)


def user_root(uid: int) -> Path:
    root = DEPLOY_DIR / str(uid)
    root.mkdir(parents=True, exist_ok=True)
    return root


@bot.message_handler(commands=["start"])
def start(message: Any) -> None:
    uid = message.from_user.id
    is_new = str(uid) not in users_db
    data = update_user_profile(message)
    if settings.get("maintenance") and uid != OWNER_ID:
        bot.send_message(message.chat.id, "⚠️ 𝑺𝒚𝒔𝒕𝒆𝒎 𝒊𝒔 𝒄𝒖𝒓𝒓𝒆𝒏𝒕𝒍𝒚 𝒖𝒏𝒅𝒆𝒓 𝒎𝒂𝒊𝒏𝒕𝒆𝒏𝒂𝒏𝒄𝒆.", reply_markup=main_keyboard(uid))
        return
    if is_new:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 2 and parts[1].isdigit():
            ref = int(parts[1])
            if ref != uid and str(ref) in users_db:
                users_db[str(ref)]["points"] = int(users_db[str(ref)].get("points", 0)) + int(settings.get("referral_points", REFERRAL_POINTS))
                try:
                    bot.send_message(ref, f"🎁 Referral bonus: +{int(settings.get('referral_points', REFERRAL_POINTS))} points.")
                except Exception:
                    pass
        save_db()

    markup = main_keyboard(uid)
    caption = welcome_text(message)
    video = settings.get("welcome_video")
    if video:
        try:
            bot.send_video(message.chat.id, video, caption=caption, reply_markup=markup)
            return
        except Exception:
            pass
    bot.send_message(message.chat.id, caption, reply_markup=markup)


@bot.message_handler(func=lambda m: m.text == "🚀 Upload File")
def upload_prompt(message: Any) -> None:
    uid = message.from_user.id
    if settings.get("maintenance") and uid != OWNER_ID:
        bot.send_message(message.chat.id, "⚠️ Hosting is under maintenance.")
        return
    data = update_user_profile(message)
    cost = int(settings.get("hosting_cost", HOSTING_COST))
    if int(data.get("points", 0)) < cost:
        bot.send_message(message.chat.id, f"❌ You need {cost} points to host a file.")
        return
    prompt = bot.send_message(message.chat.id, f"📤 Send your .py or .zip file.\n💰 Hosting cost: {cost} points")
    bot.register_next_step_handler(prompt, receive_upload)


def receive_upload(message: Any) -> None:
    if not message.document:
        bot.send_message(message.chat.id, "❌ Please send a .py or .zip document file.")
        return
    uid = message.from_user.id
    update_user_profile(message)
    filename = safe_name(message.document.file_name or "upload.py")
    ext = Path(filename).suffix.lower()
    if ext not in {".py", ".zip"}:
        bot.send_message(message.chat.id, "❌ Supported formats: .py and .zip")
        return
    file_size = int(getattr(message.document, "file_size", 0) or 0)
    if file_size and file_size > MAX_UPLOAD_MB * 1024 * 1024:
        bot.send_message(message.chat.id, f"❌ Maximum upload size is {MAX_UPLOAD_MB} MB.")
        return
    try:
        info = bot.get_file(message.document.file_id)
        content = bot.download_file(info.file_path)
    except Exception as exc:
        bot.send_message(message.chat.id, f"❌ Download failed: {clean_text(str(exc), 1000)}")
        return
    if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
        bot.send_message(message.chat.id, f"❌ Maximum upload size is {MAX_UPLOAD_MB} MB.")
        return
    progress = bot.send_message(message.chat.id, "⏳ Upload received. Preparing your bot in the background...")
    DEPLOY_EXECUTOR.submit(deployment_job, message.chat.id, uid, filename, content, progress.message_id)


@bot.message_handler(func=lambda m: m.text == "📂 My Files")
def my_files(message: Any) -> None:
    data = update_user_profile(message)
    files = data.get("files", {})
    if not files:
        bot.send_message(message.chat.id, "📂 You have no hosted files.")
        return
    for name, meta in list(files.items()):
        status = "🟢 Running" if status_of(message.from_user.id, name) else "🔴 Stopped"
        bot.send_message(
            message.chat.id,
            f"📄 {name}\n⚙️ Status: {status}",
            reply_markup=file_keyboard(message.from_user.id, name),
        )


@bot.message_handler(func=lambda m: m.text == "💰 My Points")
def points(message: Any) -> None:
    data = update_user_profile(message)
    bot.send_message(message.chat.id, f"💰 𝑷𝒐𝒊𝒏𝒕𝒔: {int(data.get('points', 0))}")


@bot.message_handler(func=lambda m: m.text == "🔗 Referral")
def referral(message: Any) -> None:
    try:
        me = bot.get_me()
        link = f"https://t.me/{me.username}?start={message.from_user.id}"
        bot.send_message(message.chat.id, f"🔗 Your referral link:\n\n{link}\n\n🎁 Bonus: {int(settings.get('referral_points', REFERRAL_POINTS))} points per new user.")
    except Exception as exc:
        bot.send_message(message.chat.id, f"❌ Could not create referral link: {clean_text(str(exc), 500)}")


@bot.message_handler(func=lambda m: m.text == "📊 Statistics")
def statistics(message: Any) -> None:
    data = update_user_profile(message)
    hosted = len(data.get("files", {}))
    running_count = sum(1 for key, proc in running.items() if key.startswith(f"{message.from_user.id}:") and proc.poll() is None)
    bot.send_message(message.chat.id, f"╭──────────────╮\n│ 📊 𝑺𝑻𝑨𝑻𝑰𝑺𝑻𝑰𝑪𝑺 │\n├──────────────┤\n│ 📦 Hosted : {hosted}\n│ 🟢 Running: {running_count}\n│ 💰 Points : {int(data.get('points', 0))}\n╰──────────────╯")


@bot.message_handler(func=lambda m: m.text == "📢 Updates")
def updates(message: Any) -> None:
    username = CHANNEL_ID.lstrip("@")
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📢 JOIN CHANNEL", url=f"https://t.me/{username}"))
    bot.send_message(message.chat.id, "📢 Follow the updates channel.", reply_markup=markup)


@bot.message_handler(func=lambda m: m.text == "👑 Contact Owner")
def contact_owner(message: Any) -> None:
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("👑 Contact @Labibtele", url="https://t.me/Labibtele"))
    bot.send_message(message.chat.id, "👑 𝑩𝒐𝒕 𝑶𝒘𝒏𝒆𝒓", reply_markup=markup)


@bot.message_handler(func=lambda m: m.text == "⚙️ Admin Panel" and m.from_user.id == OWNER_ID)
def admin_panel(message: Any) -> None:
    bot.send_message(message.chat.id, "╭────────────────────╮\n│     👑 𝑨𝑫𝑴𝑰𝑵 𝑷𝑨𝑵𝑬𝑳     │\n╰────────────────────╯", reply_markup=admin_keyboard())


# ------------------------- callbacks ---------------------------
@bot.callback_query_handler(func=lambda c: True)
def callbacks(call: Any) -> None:
    data = call.data or ""
    try:
        if data.startswith("U:"):
            try:
                page = int(data.split(":", 1)[1])
            except ValueError:
                page = 0
            bot.delete_message(call.message.chat.id, call.message.message_id)
            send_users_page(call.message.chat.id, page)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("F:"):
            token = data[2:]
            with CALLBACKS_LOCK:
                item = CALLBACKS.get(token)
            if not item:
                bot.answer_callback_query(call.id, "Expired button", show_alert=True)
                return
            action, uid, name = item
            if call.from_user.id != uid and call.from_user.id != OWNER_ID:
                bot.answer_callback_query(call.id, "Not authorized", show_alert=True)
                return
            if action == "run":
                ok, detail = start_bot(uid, name)
                bot.answer_callback_query(call.id, "Started" if ok else clean_text(detail, 180), show_alert=not ok)
            elif action == "stop":
                ok = stop_bot(uid, name)
                meta = get_meta(uid, name)
                if meta:
                    meta["status"] = "stopped"
                    meta["pid"] = None
                    save_db()
                bot.answer_callback_query(call.id, "Stopped" if ok else "Not running")
            elif action == "restart":
                stop_bot(uid, name)
                ok, detail = start_bot(uid, name)
                bot.answer_callback_query(call.id, "Restarted" if ok else clean_text(detail, 180), show_alert=not ok)
            elif action == "download":
                meta = get_meta(uid, name)
                if not meta:
                    bot.answer_callback_query(call.id, "File not found", show_alert=True)
                    return
                root = DEPLOY_DIR / meta["root"]
                if meta.get("archive"):
                    path = root / meta["archive"]
                else:
                    path = root / meta["entry"]
                if not path.is_file():
                    bot.answer_callback_query(call.id, "File missing", show_alert=True)
                    return
                with path.open("rb") as fh:
                    bot.send_document(call.message.chat.id, fh, visible_file_name=path.name)
                bot.answer_callback_query(call.id, "Sent")
            elif action == "delete":
                stop_bot(uid, name)
                meta = get_meta(uid, name)
                if meta:
                    shutil.rmtree(DEPLOY_DIR / meta["root"], ignore_errors=True)
                    ensure_user(uid)["files"].pop(name, None)
                    save_db()
                try:
                    bot.delete_message(call.message.chat.id, call.message.message_id)
                except Exception:
                    pass
                bot.answer_callback_query(call.id, "Deleted")
            return

        if call.from_user.id != OWNER_ID:
            bot.answer_callback_query(call.id, "Owner only", show_alert=True)
            return
        if data == "A:add":
            prompt = bot.send_message(call.message.chat.id, "👤 Send: USER_ID POINTS")
            bot.register_next_step_handler(prompt, add_points)
            bot.answer_callback_query(call.id)
        elif data == "A:broadcast":
            prompt = bot.send_message(call.message.chat.id, "📢 Send the broadcast message:")
            bot.register_next_step_handler(prompt, broadcast_prompt)
            bot.answer_callback_query(call.id)
        elif data == "A:setvideo":
            prompt = bot.send_message(call.message.chat.id, "🎥 Send the welcome video:")
            bot.register_next_step_handler(prompt, set_video)
            bot.answer_callback_query(call.id)
        elif data == "A:delvideo":
            settings["welcome_video"] = None
            save_settings()
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=admin_keyboard())
            bot.answer_callback_query(call.id, "Removed")
        elif data == "A:maintenance":
            settings["maintenance"] = not bool(settings.get("maintenance"))
            save_settings()
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=admin_keyboard())
            bot.answer_callback_query(call.id, "Updated")
        elif data == "A:stats":
            send_server_stats(call.message.chat.id)
            bot.answer_callback_query(call.id)
        elif data == "A:users":
            send_users_page(call.message.chat.id, 0)
            bot.answer_callback_query(call.id)
    except Exception as exc:
        logging.exception("Callback error")
        try:
            bot.answer_callback_query(call.id, "Something went wrong", show_alert=True)
        except Exception:
            pass


def add_points(message: Any) -> None:
    if message.from_user.id != OWNER_ID:
        return
    try:
        target, amount = (message.text or "").split()[:2]
        target_id = int(target)
        amount_i = int(amount)
        if amount_i < 0:
            raise ValueError
        ensure_user(target_id)["points"] = int(ensure_user(target_id).get("points", 0)) + amount_i
        save_db()
        bot.send_message(message.chat.id, f"✅ Added {amount_i} points to {target_id}.")
    except Exception:
        bot.send_message(message.chat.id, "❌ Format: USER_ID POINTS")


def broadcast_prompt(message: Any) -> None:
    if message.from_user.id != OWNER_ID:
        return
    text = (message.text or "").strip()
    if not text:
        bot.send_message(message.chat.id, "❌ Empty message.")
        return
    bot.send_message(message.chat.id, "⏳ Broadcast started in background.")
    BROADCAST_EXECUTOR.submit(run_broadcast, message.chat.id, text)


def run_broadcast(admin_chat: int, text: str) -> None:
    sent = failed = 0
    for uid in list(users_db.keys()):
        try:
            bot.send_message(int(uid), f"📢 ANNOUNCEMENT\n\n{text}")
            sent += 1
            time.sleep(0.05)
        except Exception:
            failed += 1
    try:
        bot.send_message(admin_chat, f"📢 Broadcast finished.\n\n✅ Sent: {sent}\n❌ Failed: {failed}")
    except Exception:
        pass


def set_video(message: Any) -> None:
    if message.from_user.id != OWNER_ID:
        return
    if not message.video:
        bot.send_message(message.chat.id, "❌ Please send a video.")
        return
    settings["welcome_video"] = message.video.file_id
    save_settings()
    bot.send_message(message.chat.id, "✅ Welcome video saved.")


def send_server_stats(chat_id: int) -> None:
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(str(BASE_DIR))
    active = sum(1 for p in running.values() if p.poll() is None)
    hosted = sum(len(v.get("files", {})) for v in users_db.values())
    text = (
        "╭──────────────────────╮\n"
        "│     🖥 𝑺𝑬𝑹𝑽𝑬𝑹 𝑺𝑻𝑨𝑻𝑺     │\n"
        "├──────────────────────┤\n"
        f"│ 👥 Users       : {len(users_db)}\n"
        f"│ 📦 Hosted      : {hosted}\n"
        f"│ 🟢 Running     : {active}\n"
        f"│ ⚙️ CPU         : {psutil.cpu_percent(interval=0.2):.1f}%\n"
        f"│ 🧠 RAM         : {vm.percent:.1f}%\n"
        f"│ 💾 Disk        : {disk.percent:.1f}%\n"
        f"│ 📌 Max Running : {MAX_RUNNING_BOTS}\n"
        "╰──────────────────────╯"
    )
    bot.send_message(chat_id, text)


def send_users_page(chat_id: int, page: int) -> None:
    per_page = 15
    items = []
    for uid, data in users_db.items():
        username = str(data.get("username") or "").strip()
        label = f"@{username}" if username else "No Username"
        running_count = sum(
            1 for name in data.get("files", {})
            if status_of(int(uid), name)
        )
        items.append((label, running_count, int(uid)))
    items.sort(key=lambda x: x[0].lower())
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = items[page * per_page:(page + 1) * per_page]
    lines = ["╭────────────────────────╮", "│       👤 𝑼𝑺𝑬𝑹𝑺        │", "├────────────────────────┤"]
    for label, count, _uid in chunk:
        lines.append(f"│ {label[:16]:16} • {count} Running")
    if not chunk:
        lines.append("│ No users yet.")
    total_running = sum(x[1] for x in items)
    lines += [
        "├────────────────────────┤",
        f"│ 👥 Total Users : {len(items)}",
        f"│ 🤖 Total Running: {total_running}",
        f"│ 📄 Page {page + 1}/{total_pages}",
        "╰────────────────────────╯",
    ]
    markup = types.InlineKeyboardMarkup(row_width=2)
    if page > 0:
        markup.add(types.InlineKeyboardButton("◀️ Previous", callback_data=f"U:{page-1}"))
    if page + 1 < total_pages:
        markup.add(types.InlineKeyboardButton("Next ▶️", callback_data=f"U:{page+1}"))
    bot.send_message(chat_id, "\n".join(lines), reply_markup=markup)


# ------------------------- supervisor -------------------------
def supervise() -> None:
    while True:
        time.sleep(5)
        for key, proc in list(running.items()):
            uid_s, name = key.split(":", 1)
            uid = int(uid_s)
            meta = get_meta(uid, name)
            if not meta:
                terminate_process(proc)
                running.pop(key, None)
                continue
            if proc.poll() is not None:
                running.pop(key, None)
                fh = process_logs.pop(key, None)
                if fh:
                    try:
                        fh.close()
                    except Exception:
                        pass
                meta["status"] = "crashed"
                meta["pid"] = None
                save_db()
                if meta.get("auto_restart", True) and not settings.get("maintenance"):
                    # Delay a little to prevent a crash loop from hammering CPU.
                    time.sleep(1)
                    ok, _ = start_bot(uid, name, automatic=True)
                    if ok:
                        meta["status"] = "running"
                        save_db()
                continue
            # Resource watchdog.
            try:
                parent = psutil.Process(proc.pid)
                rss = parent.memory_info().rss
                for child in parent.children(recursive=True):
                    try:
                        rss += child.memory_info().rss
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                if rss > MEMORY_LIMIT_MB * 1024 * 1024:
                    logging.warning("Stopping %s: memory limit exceeded", key)
                    terminate_process(proc)
                    meta["status"] = "stopped_resource_limit"
                    meta["pid"] = None
                    save_db()
                    continue
                if CPU_LIMIT_PERCENT > 0:
                    cpu = parent.cpu_percent(interval=0.0)
                    if cpu > CPU_LIMIT_PERCENT:
                        # One sample is not enough to kill a bot. The next loop
                        # checks again; this only records the event for now.
                        logging.warning("High CPU for %s: %.1f%%", key, cpu)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            try:
                log_path = LOG_DIR / f"{uid}_{meta.get('id', safe_name(name))}.log"
                rotate_log(log_path)
            except Exception:
                pass


def startup_recovery() -> None:
    # PIDs from a previous Railway process are never trusted. The records are
    # recreated through the normal start path, which also reinstalls dependencies.
    for uid_s, data in list(users_db.items()):
        try:
            uid = int(uid_s)
        except ValueError:
            continue
        for name, meta in list(data.get("files", {}).items()):
            meta["pid"] = None
            meta["status"] = "stopped"
            if meta.get("auto_restart", True):
                DEPLOY_EXECUTOR.submit(delayed_recovery, uid, name)
    save_db()


def delayed_recovery(uid: int, name: str) -> None:
    time.sleep(2)
    if settings.get("maintenance"):
        return
    ok, detail = start_bot(uid, name, automatic=True)
    logging.info("Recovery %s/%s: %s %s", uid, name, ok, detail[:200])


def cleanup() -> None:
    for key, proc in list(running.items()):
        try:
            terminate_process(proc)
        except Exception:
            pass
        fh = process_logs.pop(key, None)
        if fh:
            try:
                fh.close()
            except Exception:
                pass
    running.clear()


# Catch all unhandled text so the bot never crashes because of an unknown button.
@bot.message_handler(func=lambda m: True, content_types=["text"])
def unknown_text(message: Any) -> None:
    if message.text and message.text.startswith("/"):
        return
    # Do not spam users; only explain the available keyboard.
    bot.send_message(message.chat.id, "ℹ️ Please use the buttons below.", reply_markup=main_keyboard(message.from_user.id))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    logging.info("LABIB HOSTING BOT starting")
    startup_recovery()
    supervisor_thread = threading.Thread(target=supervise, name="supervisor", daemon=True)
    supervisor_thread.start()
    try:
        bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30, allowed_updates=None)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
        DEPLOY_EXECUTOR.shutdown(wait=False, cancel_futures=True)
        BROADCAST_EXECUTOR.shutdown(wait=False, cancel_futures=True)
