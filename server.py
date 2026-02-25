from fastapi import FastAPI, Request
from dotenv import load_dotenv
import httpx
import subprocess
import os
import shutil
from collections import deque
from pathlib import Path

# 优先从同目录下的 .env 文件加载环境变量
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
ALLOWED_USER_IDS_RAW = os.getenv("ALLOWED_USER_IDS", "")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None
PROCESSED_UPDATE_IDS = set()
PROCESSED_UPDATE_ORDER = deque(maxlen=300)
USER_ACTIVE_SESSION: dict[int, str | None] = {}

COPILOT_CONFIG_DIR = os.getenv("COPILOT_CONFIG_DIR") or os.path.join(str(Path.home()), ".copilot")
SESSION_STATE_DIR = os.path.join(COPILOT_CONFIG_DIR, "session-state")
COPILOT_TIMEOUT_SECONDS = int(os.getenv("COPILOT_TIMEOUT_SECONDS", "180"))
SESSION_LIST_LIMIT = int(os.getenv("SESSION_LIST_LIMIT", "10"))


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
    candidate = input_id.strip()
    if not candidate:
        return None

    all_sessions = _list_sessions(limit=200)
    if candidate in all_sessions:
        return candidate

    matches = [session_id for session_id in all_sessions if session_id.startswith(candidate)]
    if len(matches) == 1:
        return matches[0]

    return None


def _render_sessions(current_id: str | None, limit: int = 10) -> str:
    sessions = _list_sessions(limit=limit)
    if not sessions:
        return "No sessions found yet. Send a normal message first to create one."

    lines = ["Recent sessions:"]
    for session_id in sessions:
        short_id = session_id[:8]
        marker = " <= current" if current_id and session_id == current_id else ""
        lines.append(f"- {short_id} ({session_id}){marker}")
    lines.append("Usage: /use <full-id or prefix>")
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
        text = message.get("text", "")

        safe_text = text.encode("ascii", errors="backslashreplace").decode("ascii")
        _safe_log(f"[telegram] message from user_id={user_id} text='{safe_text}'")

        if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
            return {"ok": True}

        chat_id = message.get("chat", {}).get("id")
        normalized_text = text.strip()

        if normalized_text == "/new":
            USER_ACTIVE_SESSION[user_id] = None
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
        copilot_args = ["-p", text, "--allow-all-tools", "--silent"]
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
        if latest_sessions:
            USER_ACTIVE_SESSION[user_id] = latest_sessions[0]

        if chat_id:
            await _send_telegram_message(chat_id, reply_text)

        return {"ok": True}

    return _app


app = create_app()
