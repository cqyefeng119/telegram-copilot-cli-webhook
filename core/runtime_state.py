import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Literal


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
ALLOWED_USER_IDS_RAW = os.getenv("ALLOWED_USER_IDS", "")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}" if BOT_TOKEN else None
COPILOT_CONFIG_DIR = os.getenv("COPILOT_CONFIG_DIR") or os.path.join(str(Path.home()), ".copilot")
SESSION_STATE_DIR = os.path.join(COPILOT_CONFIG_DIR, "session-state")
COPILOT_TIMEOUT_SECONDS = int(os.getenv("COPILOT_TIMEOUT_SECONDS", "180"))
SESSION_LIST_LIMIT = int(os.getenv("SESSION_LIST_LIMIT", "10"))
PROJECT_SCOPE_KEY = os.getenv("PROJECT_SCOPE_KEY") or Path(os.getcwd()).name

PLAN_CONFIDENCE_THRESHOLD = _env_float("PLAN_CONFIDENCE_THRESHOLD", 0.7)
SHADOW_ENFORCEMENT_ENABLED = _env_flag("SHADOW_ENFORCEMENT_ENABLED", False)
_raw_shadow_enforcement_scope = (os.getenv("SHADOW_ENFORCEMENT_SCOPE") or "deny_only").strip().lower()
SHADOW_ENFORCEMENT_SCOPE: Literal["deny_only", "deny_and_challenge"] = (
    _raw_shadow_enforcement_scope
    if _raw_shadow_enforcement_scope in {"deny_only", "deny_and_challenge"}
    else "deny_only"
)

_WEBHOOK_DIR = Path(__file__).resolve().parent.parent
TELEGRAM_MEDIA_DIR = os.getenv("TELEGRAM_MEDIA_DIR") or str(_WEBHOOK_DIR / "media" / "received")
TELEGRAM_SENT_DIR = _WEBHOOK_DIR / "media" / "sent"
EVIDENCE_SCREENSHOT_DIR = Path(os.getenv("EVIDENCE_SCREENSHOT_DIR") or (_WEBHOOK_DIR / "media" / "evidence"))
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


_USER_SESSION_PERSIST_FILE = _WEBHOOK_DIR / "user_sessions.json"
_STORE_FILE = _WEBHOOK_DIR / "approval_store.json"
_AUDIT_FILE = _WEBHOOK_DIR / "audit_log.jsonl"


def _empty_store() -> dict[str, Any]:
    return {
        "user_sessions": {},
        "user_agents": {},
        "user_models": {},
        "grants": [],
        "domain_grants": [],
        "pending": {},
    }


def _load_store() -> dict[str, Any]:
    try:
        if _STORE_FILE.exists():
            data = json.loads(_STORE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = _empty_store()
                base.update(data)
                if not isinstance(base.get("grants"), list):
                    base["grants"] = []
                if not isinstance(base.get("pending"), dict):
                    base["pending"] = {}
                if not isinstance(base.get("user_sessions"), dict):
                    base["user_sessions"] = {}
                if not isinstance(base.get("user_agents"), dict):
                    base["user_agents"] = {}
                if not isinstance(base.get("user_models"), dict):
                    base["user_models"] = {}
                if not isinstance(base.get("domain_grants"), list):
                    base["domain_grants"] = []
                return base
    except Exception as exc:
        _safe_log(f"[store] failed to load store: {exc}")

    base = _empty_store()
    try:
        if _USER_SESSION_PERSIST_FILE.exists():
            legacy = json.loads(_USER_SESSION_PERSIST_FILE.read_text(encoding="utf-8"))
            if isinstance(legacy, dict):
                base["user_sessions"] = {str(k): v for k, v in legacy.items()}
    except Exception as exc:
        _safe_log(f"[store] failed to migrate legacy sessions: {exc}")
    return base


def _save_store() -> None:
    try:
        _STORE_FILE.write_text(
            json.dumps(_RUNTIME_STORE, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        _safe_log(f"[store] failed to save: {exc}")


def _append_audit(event: str, payload: dict[str, Any]) -> None:
    try:
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            **payload,
        }
        with _AUDIT_FILE.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        _safe_log(f"[audit] failed to append: {exc}")


_RUNTIME_STORE = _load_store()


def _load_user_sessions() -> dict[int, str | None]:
    try:
        data = _RUNTIME_STORE.get("user_sessions") or {}
        return {int(k): v for k, v in data.items()}
    except Exception:
        pass
    return {}


def _save_user_sessions(sessions: dict[int, str | None]) -> None:
    try:
        _RUNTIME_STORE["user_sessions"] = {str(k): v for k, v in sessions.items()}
        _save_store()
    except Exception as exc:
        _safe_log(f"[session persist] failed to save: {exc}")


USER_ACTIVE_SESSION: dict[int, str | None] = _load_user_sessions()


def _load_user_agents() -> dict[int, str | None]:
    try:
        data = _RUNTIME_STORE.get("user_agents") or {}
        return {int(k): v for k, v in data.items()}
    except Exception:
        pass
    return {}


def _save_user_agents(agents: dict[int, str | None]) -> None:
    try:
        _RUNTIME_STORE["user_agents"] = {str(k): v for k, v in agents.items()}
        _save_store()
    except Exception as exc:
        _safe_log(f"[agent persist] failed to save: {exc}")


USER_ACTIVE_AGENT: dict[int, str | None] = _load_user_agents()


def _load_user_models() -> dict[int, str | None]:
    try:
        data = _RUNTIME_STORE.get("user_models") or {}
        return {int(k): v for k, v in data.items()}
    except Exception:
        pass
    return {}


def _save_user_models(models: dict[int, str | None]) -> None:
    try:
        _RUNTIME_STORE["user_models"] = {str(k): v for k, v in models.items()}
        _save_store()
    except Exception as exc:
        _safe_log(f"[model persist] failed to save: {exc}")


USER_ACTIVE_MODEL: dict[int, str | None] = _load_user_models()


def _default_screenshot_dirs() -> list[str]:
    return [
        str(EVIDENCE_SCREENSHOT_DIR),
        str(_WEBHOOK_DIR / "media"),
        str(_WEBHOOK_DIR),
    ]


def _parse_screenshot_dirs() -> list[str]:
    configured = os.getenv("SCREENSHOT_DIRS", "").strip()
    if not configured:
        return _default_screenshot_dirs()
    return [part.strip() for part in configured.split(os.pathsep) if part.strip()]


SCREENSHOT_DIRS = _parse_screenshot_dirs()