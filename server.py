from fastapi import FastAPI, Request
from dotenv import load_dotenv
import httpx
import subprocess
import os
import shutil
import re
import yaml
from collections import deque
from pathlib import Path
from datetime import datetime

# Prefer loading environment variables from the .env file in the same directory
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
ALLOWED_USER_IDS_RAW = os.getenv("ALLOWED_USER_IDS", "")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}" if BOT_TOKEN else None
PROCESSED_UPDATE_IDS = set()
PROCESSED_UPDATE_ORDER = deque(maxlen=300)
COPILOT_CONFIG_DIR = os.getenv("COPILOT_CONFIG_DIR") or os.path.join(str(Path.home()), ".copilot")
SESSION_STATE_DIR = os.path.join(COPILOT_CONFIG_DIR, "session-state")
COPILOT_TIMEOUT_SECONDS = int(os.getenv("COPILOT_TIMEOUT_SECONDS", "180"))
SESSION_LIST_LIMIT = int(os.getenv("SESSION_LIST_LIMIT", "10"))

_WEBHOOK_DIR = Path(__file__).parent
TELEGRAM_MEDIA_DIR = os.getenv("TELEGRAM_MEDIA_DIR") or str(_WEBHOOK_DIR / "media" / "received")
TELEGRAM_SENT_DIR = _WEBHOOK_DIR / "media" / "sent"
TELEGRAM_IMAGE_MAX_BYTES = int(os.getenv("TELEGRAM_IMAGE_MAX_BYTES", "10485760"))

DEFAULT_TELEGRAM_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
RAW_TELEGRAM_IMAGE_EXTENSIONS = os.getenv("TELEGRAM_IMAGE_EXTENSIONS", "")
if RAW_TELEGRAM_IMAGE_EXTENSIONS.strip():
    TELEGRAM_IMAGE_EXTENSIONS = {
        (ext.strip().lower() if ext.strip().startswith(".") else f".{ext.strip().lower()}")
        for ext in RAW_TELEGRAM_IMAGE_EXTENSIONS.split(",")
        if ext.strip()
    }
else:
    TELEGRAM_IMAGE_EXTENSIONS = DEFAULT_TELEGRAM_IMAGE_EXTENSIONS

DEFAULT_SCREENSHOT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
RAW_SCREENSHOT_EXTENSIONS = os.getenv("SCREENSHOT_EXTENSIONS", "")
if RAW_SCREENSHOT_EXTENSIONS.strip():
    SCREENSHOT_EXTENSIONS = {
        (ext.strip().lower() if ext.strip().startswith(".") else f".{ext.strip().lower()}")
        for ext in RAW_SCREENSHOT_EXTENSIONS.split(",")
        if ext.strip()
    }
else:
    SCREENSHOT_EXTENSIONS = DEFAULT_SCREENSHOT_EXTENSIONS

EVIDENCE_TRIGGER_PATTERNS = [
    re.compile(r"\b(send|show|attach|provide)\b.{0,40}\b(screenshot|evidence|proof)\b", re.IGNORECASE),
    re.compile(r"\b(screenshot|evidence|proof)\b.{0,40}\b(send|show|attach|provide)\b", re.IGNORECASE),
    # Chinese patterns: 截图/证据/截屏 combined with 发/给/看/展示
    re.compile(r"(截图|截屏|屏幕截图).{0,20}(发|给|看|展示|作为证据)", re.IGNORECASE),
    re.compile(r"(发|给|展示).{0,20}(截图|截屏|屏幕截图|证据)", re.IGNORECASE),
    re.compile(r"作为证据", re.IGNORECASE),
]


def _parse_allowed_user_ids() -> set[int]:
    allowed_ids: set[int] = set()
    if ALLOWED_USER_ID > 0:
        allowed_ids.add(ALLOWED_USER_ID)

    for part in ALLOWED_USER_IDS_RAW.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            allowed_ids.add(int(candidate))
        except ValueError:
            continue

    return allowed_ids


ALLOWED_USER_IDS = _parse_allowed_user_ids()


def _safe_log(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        safe_message = message.encode("ascii", errors="backslashreplace").decode("ascii")
        print(safe_message)


_USER_SESSION_PERSIST_FILE = Path(__file__).parent / "user_sessions.json"


def _load_user_sessions() -> dict[int, str | None]:
    try:
        if _USER_SESSION_PERSIST_FILE.exists():
            import json
            data = json.loads(_USER_SESSION_PERSIST_FILE.read_text(encoding="utf-8"))
            return {int(k): v for k, v in data.items()}
    except Exception:
        pass
    return {}


def _save_user_sessions(sessions: dict[int, str | None]) -> None:
    try:
        import json
        _USER_SESSION_PERSIST_FILE.write_text(
            json.dumps({str(k): v for k, v in sessions.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        _safe_log(f"[session persist] failed to save: {exc}")


USER_ACTIVE_SESSION: dict[int, str | None] = _load_user_sessions()


def _default_screenshot_dirs()-> list[str]:
    home = str(Path.home())
    one_drive = os.getenv("OneDrive")
    dirs = [
        str(_WEBHOOK_DIR),  # playwright screenshots are saved here
        os.path.join(home, "Pictures", "Screenshots"),
        os.path.join(home, "Desktop"),
        os.getcwd(),
    ]
    if one_drive:
        dirs.append(os.path.join(one_drive, "Pictures", "Screenshots"))
    return dirs


def _parse_screenshot_dirs() -> list[str]:
    configured = os.getenv("SCREENSHOT_DIRS", "").strip()
    if not configured:
        return _default_screenshot_dirs()
    return [part.strip() for part in configured.split(os.pathsep) if part.strip()]


SCREENSHOT_DIRS = _parse_screenshot_dirs()


def _resolve_copilot_command() -> list[str] | None:
    configured = os.getenv("COPILOT_COMMAND")
    if configured:
        return [configured]

    userprofile = os.getenv("USERPROFILE")
    if userprofile:
        vscode_ps1 = os.path.join(
            userprofile,
            "AppData",
            "Roaming",
            "Code",
            "User",
            "globalStorage",
            "github.copilot-chat",
            "copilotCli",
            "copilot.ps1",
        )
        if os.path.exists(vscode_ps1):
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", vscode_ps1]

    detected = shutil.which("copilot")
    if detected:
        return [detected]

    appdata = os.getenv("APPDATA")
    if appdata:
        npm_cmd = os.path.join(appdata, "npm", "copilot.cmd")
        if os.path.exists(npm_cmd):
            return [npm_cmd]

    return None


def _is_duplicate_update(update_id: int | None) -> bool:
    if update_id is None:
        return False

    if update_id in PROCESSED_UPDATE_IDS:
        return True

    if len(PROCESSED_UPDATE_ORDER) == PROCESSED_UPDATE_ORDER.maxlen:
        old_id = PROCESSED_UPDATE_ORDER.popleft()
        PROCESSED_UPDATE_IDS.discard(old_id)

    PROCESSED_UPDATE_ORDER.append(update_id)
    PROCESSED_UPDATE_IDS.add(update_id)
    return False


def _list_sessions(limit: int = 10) -> list[str]:
    session_dir = Path(SESSION_STATE_DIR)
    if not session_dir.exists():
        return []

    items = [item for item in session_dir.iterdir() if item.is_dir()]
    items.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return [item.name for item in items[:limit]]


def _resolve_session_id(input_id: str) -> str | None:
    candidate = input_id.strip().lstrip("#")
    if not candidate:
        return None

    # Support numeric index from /sessions list (e.g. "1", "#2")
    if candidate.isdigit():
        idx = int(candidate) - 1
        all_items = _list_sessions(limit=200)
        if 0 <= idx < len(all_items):
            return all_items[idx]
        return None

    all_sessions = _list_sessions(limit=200)
    if candidate in all_sessions:
        return candidate

    matches = [session_id for session_id in all_sessions if session_id.startswith(candidate)]
    if len(matches) == 1:
        return matches[0]

    return None


def _get_session_title(session_id: str) -> str | None:
    """Extract session title from workspace.yaml summary field."""
    yaml_file = Path(SESSION_STATE_DIR) / session_id / "workspace.yaml"
    if not yaml_file.exists():
        return None
    try:
        with yaml_file.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        summary = data.get("summary", "") or ""
        for line in summary.splitlines():
            line = line.strip()
            # prefer heading lines
            m = re.match(r"^#{1,3}\s+(.+)", line)
            if m:
                return m.group(1).strip()
        # fall back to first non-empty line
        for line in summary.splitlines():
            line = line.strip(" #-*\t")
            if line:
                return line[:60]
    except Exception:
        pass
    return None


def _relative_time(mtime: float) -> str:
    """Return a human-readable relative time string for a Unix mtime."""
    delta = datetime.now().timestamp() - mtime
    if delta < 60:
        return "just now"
    if delta < 3600:
        mins = int(delta // 60)
        return f"{mins}m ago"
    if delta < 86400:
        hours = int(delta // 3600)
        return f"{hours}h ago"
    days = int(delta // 86400)
    return f"{days}d ago"


def _render_sessions(current_id: str | None, limit: int = 10) -> str:
    session_dir = Path(SESSION_STATE_DIR)
    items = [item for item in session_dir.iterdir() if item.is_dir()] if session_dir.exists() else []
    items.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    items = items[:limit]

    if not items:
        return "No sessions found yet. Send a normal message first to create one."

    lines = ["Recent sessions:"]
    for idx, item in enumerate(items, start=1):
        session_id = item.name
        marker = " ✅" if current_id and session_id == current_id else ""
        rel_time = _relative_time(item.stat().st_mtime)
        title = _get_session_title(session_id)
        title_part = f"  {title}" if title else ""
        lines.append(f"{idx}. `{session_id[:8]}` ({rel_time}){marker}{title_part}")
    lines.append("\nUsage: /use <number>  e.g. /use 2")
    return "\n".join(lines)


def _render_help() -> str:
    return "\n".join(
        [
            "Commands:",
            "/help - Show help",
            "/new - Start a fresh session (next normal message creates a new session)",
            "/sessions - List recent sessions",
            "/use <id> - Switch to a specific session (prefix is supported)",
            "",
            "Default behavior:",
            "- A normal message continues the current session.",
            "- If no current session exists, the first normal message creates one.",
        ]
    )


def _split_message(text: str, max_len: int = 3500) -> list[str]:
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text
    while len(remaining) > max_len:
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def _send_telegram_message(chat_id: int, text: str) -> None:
    if not TELEGRAM_API:
        return

    chunks = _split_message(text)
    async with httpx.AsyncClient(timeout=30) as client:
        for chunk in chunks:
            payload = {
                "chat_id": chat_id,
                "text": chunk,
            }
            resp = await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)
            if resp.status_code >= 400:
                _safe_log(f"[telegram send error] {resp.status_code} {resp.text}")


def _is_explicit_evidence_request(text: str) -> bool:
    candidate = (text or "").strip()
    if not candidate:
        return False
    return any(pattern.search(candidate) for pattern in EVIDENCE_TRIGGER_PATTERNS)


def _find_latest_screenshot() -> Path | None:
    latest_path: Path | None = None
    latest_mtime = -1.0

    for raw_dir in SCREENSHOT_DIRS:
        directory = Path(raw_dir)
        if not directory.exists() or not directory.is_dir():
            continue

        for item in directory.iterdir():
            if not item.is_file():
                continue
            if item.suffix.lower() not in SCREENSHOT_EXTENSIONS:
                continue
            try:
                mtime = item.stat().st_mtime
            except OSError:
                continue
            if mtime > latest_mtime:
                latest_path = item
                latest_mtime = mtime

    return latest_path


async def _send_telegram_photo(chat_id: int, file_path: Path) -> bool:
    if not TELEGRAM_API or not file_path.exists() or not file_path.is_file():
        return False

    async with httpx.AsyncClient(timeout=60) as client:
        with file_path.open("rb") as file_obj:
            resp = await client.post(
                f"{TELEGRAM_API}/sendPhoto",
                data={"chat_id": str(chat_id)},
                files={"photo": (file_path.name, file_obj, "application/octet-stream")},
            )
        if resp.status_code < 400:
            return True

        _safe_log(f"[telegram sendPhoto error] {resp.status_code} {resp.text}")
        with file_path.open("rb") as file_obj:
            fallback_resp = await client.post(
                f"{TELEGRAM_API}/sendDocument",
                data={"chat_id": str(chat_id)},
                files={"document": (file_path.name, file_obj, "application/octet-stream")},
            )
        if fallback_resp.status_code < 400:
            return True

        _safe_log(f"[telegram sendDocument error] {fallback_resp.status_code} {fallback_resp.text}")
        return False


def _extract_telegram_image_candidate(message: dict) -> dict | None:
    photos = message.get("photo") or []
    if photos:
        sorted_photos = sorted(photos, key=lambda item: int(item.get("file_size", 0) or 0))
        largest = sorted_photos[-1]
        return {
            "file_id": largest.get("file_id"),
            "file_size": int(largest.get("file_size", 0) or 0),
            "file_name": f"telegram_photo_{largest.get('file_unique_id', 'image')}.jpg",
        }

    document = message.get("document") or {}
    if document.get("mime_type", "").startswith("image/"):
        return {
            "file_id": document.get("file_id"),
            "file_size": int(document.get("file_size", 0) or 0),
            "file_name": document.get("file_name") or f"telegram_document_{document.get('file_unique_id', 'image')}",
        }

    return None


def _sanitize_extension(file_name: str | None, allowed_extensions: set[str], fallback: str = ".jpg") -> str:
    ext = Path(file_name or "").suffix.lower()
    if ext in allowed_extensions:
        return ext
    return fallback


async def _download_telegram_image(user_id: int, message: dict) -> Path | None:
    if not TELEGRAM_API or not TELEGRAM_FILE_API:
        return None

    candidate = _extract_telegram_image_candidate(message)
    if not candidate:
        return None

    file_id = candidate.get("file_id")
    if not file_id:
        return None

    declared_size = int(candidate.get("file_size", 0) or 0)
    if declared_size > TELEGRAM_IMAGE_MAX_BYTES:
        _safe_log(f"[telegram image skipped] declared size too large: {declared_size} > {TELEGRAM_IMAGE_MAX_BYTES}")
        return None

    extension = _sanitize_extension(candidate.get("file_name"), TELEGRAM_IMAGE_EXTENSIONS)

    async with httpx.AsyncClient(timeout=60) as client:
        get_file_resp = await client.post(f"{TELEGRAM_API}/getFile", json={"file_id": file_id})
        if get_file_resp.status_code >= 400:
            _safe_log(f"[telegram getFile error] {get_file_resp.status_code} {get_file_resp.text}")
            return None

        payload = get_file_resp.json()
        if not payload.get("ok"):
            _safe_log(f"[telegram getFile error] unexpected payload: {payload}")
            return None

        remote_path = (payload.get("result") or {}).get("file_path")
        if not remote_path:
            return None

        media_dir = Path(TELEGRAM_MEDIA_DIR) / str(user_id)
        media_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        local_path = media_dir / f"{timestamp}{extension}"

        download_resp = await client.get(f"{TELEGRAM_FILE_API}/{remote_path}")
        if download_resp.status_code >= 400:
            _safe_log(f"[telegram file download error] {download_resp.status_code} {download_resp.text}")
            return None

        content = download_resp.content
        if len(content) > TELEGRAM_IMAGE_MAX_BYTES:
            _safe_log(f"[telegram image skipped] actual size too large: {len(content)} > {TELEGRAM_IMAGE_MAX_BYTES}")
            return None

        local_path.write_bytes(content)
        return local_path


def create_app() -> FastAPI:
    _app = FastAPI()

    @_app.post("/webhook/{token}")
    async def telegram_webhook(token: str, req: Request):
        if token != BOT_TOKEN:
            return {"ok": False, "error": "invalid token"}

        data = await req.json()
        update_id = data.get("update_id")
        if _is_duplicate_update(update_id):
            _safe_log(f"[telegram] duplicated update_id={update_id}, skipped")
            return {"ok": True}

        message = data.get("message")
        if not message:
            return {"ok": True}

        user_id = message.get("from", {}).get("id")
        text = message.get("text") or message.get("caption", "")

        safe_text = text.encode("ascii", errors="backslashreplace").decode("ascii")
        _safe_log(f"[telegram] message from user_id={user_id} text='{safe_text}'")

        if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
            return {"ok": True}

        chat_id = message.get("chat", {}).get("id")
        normalized_text = text.strip()

        if normalized_text == "/new":
            USER_ACTIVE_SESSION[user_id] = None
            _save_user_sessions(USER_ACTIVE_SESSION)
            if chat_id:
                await _send_telegram_message(chat_id, "Switched to new-session mode. Your next normal message will create a new session.")
            return {"ok": True}

        if normalized_text == "/help":
            if chat_id:
                await _send_telegram_message(chat_id, _render_help())
            return {"ok": True}

        if normalized_text == "/sessions":
            current_session = USER_ACTIVE_SESSION.get(user_id)
            if chat_id:
                await _send_telegram_message(chat_id, _render_sessions(current_session, limit=SESSION_LIST_LIMIT))
            return {"ok": True}

        if normalized_text.startswith("/use"):
            parts = normalized_text.split(maxsplit=1)
            if len(parts) < 2:
                if chat_id:
                    await _send_telegram_message(chat_id, "Usage: /use <session-id or prefix>")
                return {"ok": True}

            resolved = _resolve_session_id(parts[1])
            if not resolved:
                if chat_id:
                    await _send_telegram_message(chat_id, "No unique matching session found. Run /sessions first.")
                return {"ok": True}

            USER_ACTIVE_SESSION[user_id] = resolved
            _save_user_sessions(USER_ACTIVE_SESSION)
            if chat_id:
                await _send_telegram_message(chat_id, f"Switched to session: {resolved}")
            return {"ok": True}

        copilot_cmd = _resolve_copilot_command()
        if not copilot_cmd:
            reply_text = (
                "Copilot CLI was not found. Ensure it is installed, or set in .env:\n"
                "COPILOT_COMMAND=C:\\path\\to\\copilot.ps1"
            )
            if chat_id:
                await _send_telegram_message(chat_id, reply_text)
            return {"ok": True}

        current_session = USER_ACTIVE_SESSION.get(user_id)
        evidence_requested = _is_explicit_evidence_request(text)

        local_image_path = await _download_telegram_image(user_id, message)
        copilot_prompt = text
        if local_image_path:
            if copilot_prompt.strip():
                copilot_prompt = f"{copilot_prompt}\n\n[Telegram image path]\n{local_image_path}"
            else:
                copilot_prompt = f"Analyze the attached Telegram image at path: {local_image_path}"

        # Snapshot sessions before running so we can detect newly created ones
        sessions_before: set[str] = set(_list_sessions(limit=200))

        copilot_args = ["-p", copilot_prompt, "--allow-all-tools", "--silent"]
        if current_session:
            copilot_args = ["--resume", current_session] + copilot_args

        try:
            result = subprocess.run(
                copilot_cmd + copilot_args,
                capture_output=True,
                text=False,
                env=os.environ.copy(),
                timeout=COPILOT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            _safe_log(f"[copilot timeout] command exceeded {COPILOT_TIMEOUT_SECONDS} seconds")
            chat_id = message.get("chat", {}).get("id")
            if chat_id:
                await _send_telegram_message(chat_id, f"Copilot timed out after {COPILOT_TIMEOUT_SECONDS}s. Try a shorter request.")
            return {"ok": True}
        except Exception as exc:
            _safe_log(f"[copilot launch error] {exc}")
            chat_id = message.get("chat", {}).get("id")
            if chat_id:
                await _send_telegram_message(chat_id, f"Copilot failed to start: {exc}")
            return {"ok": True}

        stdout = (result.stdout or b"").decode("utf-8", errors="replace").strip()
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()

        _safe_log(f"[copilot stdout] {stdout}")
        if result.returncode != 0:
            _safe_log(f"[copilot stderr] {stderr}")

        if result.returncode == 0 and stdout:
            reply_text = stdout
        elif stderr:
            reply_text = f"Copilot command failed:\n{stderr}"
        else:
            reply_text = "Copilot returned no output."

        latest_sessions = _list_sessions(limit=1)
        if current_session:
            # Resumed an existing session — keep it
            USER_ACTIVE_SESSION[user_id] = current_session
        else:
            # New session mode — find the session that was just created
            sessions_after = _list_sessions(limit=200)
            new_sessions = [s for s in sessions_after if s not in sessions_before]
            if new_sessions:
                USER_ACTIVE_SESSION[user_id] = new_sessions[0]
            elif latest_sessions:
                USER_ACTIVE_SESSION[user_id] = latest_sessions[0]
        _save_user_sessions(USER_ACTIVE_SESSION)

        if chat_id:
            await _send_telegram_message(chat_id, reply_text)
            if evidence_requested:
                try:
                    latest_screenshot = _find_latest_screenshot()
                    if latest_screenshot:
                        sent = await _send_telegram_photo(chat_id, latest_screenshot)
                        if sent:
                            try:
                                TELEGRAM_SENT_DIR.mkdir(parents=True, exist_ok=True)
                                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                                dest = TELEGRAM_SENT_DIR / f"{timestamp}_{latest_screenshot.name}"
                                shutil.move(str(latest_screenshot), dest)
                            except Exception as move_exc:
                                _safe_log(f"[screenshot archive] failed to move: {move_exc}")
                    else:
                        _safe_log("[evidence] explicit request detected, but no screenshot found")
                except Exception as exc:
                    _safe_log(f"[evidence send error] {exc}")

        return {"ok": True}

    return _app


app = create_app()
