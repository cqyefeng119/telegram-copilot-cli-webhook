from fastapi import FastAPI, Request
from dotenv import load_dotenv
import httpx
import subprocess
import os
import shutil
import re
import yaml
import json
import secrets
from collections import deque
from pathlib import Path
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

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
PROJECT_SCOPE_KEY = os.getenv("PROJECT_SCOPE_KEY") or Path(os.getcwd()).name


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


PLAN_FIRST_MODE = _env_flag("PLAN_FIRST_MODE", True)
PLAN_CONFIDENCE_THRESHOLD = _env_float("PLAN_CONFIDENCE_THRESHOLD", 0.7)
EVIDENCE_LLM_ENABLED = _env_flag("EVIDENCE_LLM_ENABLED", True)

_WEBHOOK_DIR = Path(__file__).parent
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

EVIDENCE_TRIGGER_PATTERNS = [
    re.compile(r"\b(send|show|attach|provide)\b.{0,40}\b(screenshot|evidence|proof)\b", re.IGNORECASE),
    re.compile(r"\b(screenshot|evidence|proof)\b.{0,40}\b(send|show|attach|provide)\b", re.IGNORECASE),
    # Chinese patterns: 截图/证据/截屏 combined with 发/给/看/展示
    re.compile(r"(截图|截屏|屏幕截图).{0,20}(发|给|看|展示|作为证据)", re.IGNORECASE),
    re.compile(r"(发|给|展示).{0,20}(截图|截屏|屏幕截图|证据)", re.IGNORECASE),
    re.compile(r"作为证据", re.IGNORECASE),
]

HIGH_RISK_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("destructive", re.compile(r"\b(rm|remove|del|delete|drop|truncate|format|wipe|shutdown|reboot|kill)\b|删除|删掉|移除|清空|格式化|抹掉|削除|消去", re.IGNORECASE)),
    ("repo_write", re.compile(r"\b(git\s+push|git\s+commit|git\s+reset|git\s+rebase|force\s+push)\b", re.IGNORECASE)),
    ("secret", re.compile(r"\b(password|secret|token|credential|api\s*key)\b", re.IGNORECASE)),
]

_URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_BARE_DOMAIN_PATTERN = re.compile(
    r"(?<![@\w-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}(?::\d{1,5})?(?:/[^\s<>'\"]*)?",
    re.IGNORECASE,
)


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
_STORE_FILE = Path(__file__).parent / "approval_store.json"
_AUDIT_FILE = Path(__file__).parent / "audit_log.jsonl"


def _empty_store() -> dict[str, Any]:
    return {
        "user_sessions": {},
        "user_agents": {},
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


def _classify_risk(text: str) -> tuple[bool, str, str]:
    candidate = (text or "").strip()
    for risk_kind, pattern in HIGH_RISK_RULES:
        if pattern.search(candidate):
            return True, risk_kind, pattern.pattern
    return False, "general", "none"


def _normalize_domain(raw: str) -> str | None:
    candidate = (raw or "").strip().lower()
    if not candidate:
        return None

    if candidate.startswith("http://") or candidate.startswith("https://"):
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").strip().lower().strip(".")
    else:
        parsed = urlparse(f"http://{candidate}")
        host = (parsed.hostname or "").strip().lower().strip(".")

    if not host:
        return None
    if host == "localhost":
        return host
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        return host
    if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", host):
        return None
    return host


def _extract_domains_from_text(text: str) -> list[str]:
    candidate = (text or "").strip()
    if not candidate:
        return []

    domains: list[str] = []
    seen: set[str] = set()

    for match in _URL_PATTERN.findall(candidate):
        normalized = _normalize_domain(match)
        if normalized and normalized not in seen:
            seen.add(normalized)
            domains.append(normalized)

    for match in _BARE_DOMAIN_PATTERN.findall(candidate):
        normalized = _normalize_domain(match)
        if normalized and normalized not in seen:
            seen.add(normalized)
            domains.append(normalized)

    return domains


def _build_plan_prompt(user_prompt: str) -> str:
    return (
        "You are an action planner. Return ONLY JSON, no markdown, no prose.\\n"
        "Analyze the user request and produce this schema:\\n"
        "{\\n"
        "  \"actions\": [{\"type\": \"network|destructive|repo_write|secret|read|other\", \"summary\": \"...\"}],\\n"
        "  \"domains\": [\"example.com\"],\\n"
        "  \"needs_evidence\": true,\\n"
        "  \"confidence\": 0.0\\n"
        "}\\n"
        "Rules:\\n"
        "- confidence must be between 0 and 1.\\n"
        "- domains should include only concrete domains/hosts if present.\\n"
        "- action type must be one of the allowed enum values.\\n"
        "- Keep summaries concise.\\n"
        "User request:\\n"
        f"{user_prompt}"
    )


def _parse_action_plan(raw_text: str) -> dict[str, Any]:
    default_plan: dict[str, Any] = {
        "actions": [],
        "domains": [],
        "needs_evidence": False,
        "confidence": 0.0,
        "parse_ok": False,
    }
    candidate = (raw_text or "").strip()
    if not candidate:
        return default_plan

    if "```" in candidate:
        fence_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", candidate, re.IGNORECASE)
        if fence_match:
            candidate = fence_match.group(1)

    left = candidate.find("{")
    right = candidate.rfind("}")
    if left >= 0 and right > left:
        candidate = candidate[left : right + 1]

    parsed: Any = None
    try:
        parsed = json.loads(candidate)
    except Exception:
        try:
            parsed = yaml.safe_load(candidate)
        except Exception:
            return default_plan

    if not isinstance(parsed, dict):
        return default_plan

    allowed_types = {"network", "destructive", "repo_write", "secret", "read", "other"}
    normalized_actions: list[dict[str, str]] = []
    for item in parsed.get("actions") or []:
        if isinstance(item, dict):
            action_type = str(item.get("type") or "other").strip().lower()
            summary = str(item.get("summary") or "").strip()
        else:
            action_type = "other"
            summary = str(item).strip()
        if action_type not in allowed_types:
            action_type = "other"
        if summary:
            normalized_actions.append({"type": action_type, "summary": summary})

    normalized_domains: list[str] = []
    seen_domains: set[str] = set()
    for raw_domain in parsed.get("domains") or []:
        normalized = _normalize_domain(str(raw_domain))
        if normalized and normalized not in seen_domains:
            seen_domains.add(normalized)
            normalized_domains.append(normalized)

    confidence = 0.0
    raw_confidence = parsed.get("confidence")
    try:
        confidence = float(raw_confidence)
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    needs_evidence = bool(parsed.get("needs_evidence", False))

    return {
        "actions": normalized_actions,
        "domains": normalized_domains,
        "needs_evidence": needs_evidence,
        "confidence": confidence,
        "parse_ok": True,
    }


def _plan_actions_with_copilot(
    copilot_cmd: list[str],
    user_prompt: str,
    current_session: str | None,
    agent_name: str | None,
) -> dict[str, Any]:
    plan_prompt = _build_plan_prompt(user_prompt)
    copilot_args = ["-p", plan_prompt]
    if agent_name:
        copilot_args.extend(["--agent", agent_name])
    if current_session:
        copilot_args = ["--resume", current_session] + copilot_args

    try:
        result = subprocess.run(
            copilot_cmd + copilot_args,
            capture_output=True,
            text=False,
            env=os.environ.copy(),
            timeout=min(COPILOT_TIMEOUT_SECONDS, 90),
        )
    except Exception as exc:
        _safe_log(f"[plan] copilot execution failed: {exc}")
        return {
            "actions": [],
            "domains": [],
            "needs_evidence": False,
            "confidence": 0.0,
            "parse_ok": False,
            "error": str(exc),
        }

    stdout = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        _safe_log(f"[plan] copilot failed returncode={result.returncode}; stderr={stderr}")
        return {
            "actions": [],
            "domains": [],
            "needs_evidence": False,
            "confidence": 0.0,
            "parse_ok": False,
            "error": stderr or f"return_code={result.returncode}",
        }

    parsed = _parse_action_plan(stdout)
    parsed["raw"] = stdout
    return parsed


def _iter_allow_grants() -> list[dict[str, Any]]:
    grants = _RUNTIME_STORE.get("grants") or []
    if not isinstance(grants, list):
        return []
    return [g for g in grants if isinstance(g, dict) and g.get("allow") is True]


def _match_grant(grant: dict[str, Any], scope: str, scope_id: str, risk_kind: str) -> bool:
    if grant.get("scope") != scope:
        return False
    if str(grant.get("scope_id") or "") != scope_id:
        return False
    grant_risk = str(grant.get("risk") or "")
    return grant_risk == risk_kind or grant_risk == "*"


def _resolve_allow_scope(user_id: int, agent_name: str | None, current_session: str | None, risk_kind: str) -> str | None:
    scopes = [
        ("user", str(user_id)),
        ("agent", agent_name or ""),
        ("project", PROJECT_SCOPE_KEY),
        ("conversation", current_session or "new-session"),
    ]
    grants = _iter_allow_grants()
    for scope, scope_id in scopes:
        if not scope_id:
            continue
        if any(_match_grant(grant, scope, scope_id, risk_kind) for grant in grants):
            return scope
    return None


def _upsert_allow_grant(scope: str, scope_id: str, risk_kind: str, by_user_id: int) -> None:
    if not scope_id:
        return
    grants = _iter_allow_grants()
    for grant in grants:
        if _match_grant(grant, scope, scope_id, risk_kind):
            return
    grants.append(
        {
            "scope": scope,
            "scope_id": scope_id,
            "risk": risk_kind,
            "allow": True,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "by_user_id": by_user_id,
        }
    )
    _RUNTIME_STORE["grants"] = grants
    _save_store()


def _iter_domain_grants() -> list[dict[str, Any]]:
    grants = _RUNTIME_STORE.get("domain_grants") or []
    if not isinstance(grants, list):
        return []
    return [
        grant
        for grant in grants
        if isinstance(grant, dict) and grant.get("allow") is True and _normalize_domain(str(grant.get("domain") or ""))
    ]


def _match_domain_grant(grant: dict[str, Any], scope: str, scope_id: str, domain: str) -> bool:
    if grant.get("scope") != scope:
        return False
    if str(grant.get("scope_id") or "") != scope_id:
        return False

    granted_domain = _normalize_domain(str(grant.get("domain") or ""))
    requested_domain = _normalize_domain(domain)
    if not granted_domain or not requested_domain:
        return False
    return requested_domain == granted_domain or requested_domain.endswith(f".{granted_domain}")


def _resolve_domain_allow_scope(
    user_id: int,
    agent_name: str | None,
    current_session: str | None,
    domains: list[str],
) -> str | None:
    normalized_domains = [domain for domain in (_normalize_domain(item) for item in domains) if domain]
    if not normalized_domains:
        return None

    scopes = [
        ("user", str(user_id)),
        ("agent", agent_name or ""),
        ("project", PROJECT_SCOPE_KEY),
        ("conversation", current_session or "new-session"),
    ]
    grants = _iter_domain_grants()

    for scope, scope_id in scopes:
        if not scope_id:
            continue
        if all(any(_match_domain_grant(grant, scope, scope_id, domain) for grant in grants) for domain in normalized_domains):
            return scope

    if all(
        any(
            _match_domain_grant(grant, scope, scope_id, domain)
            for scope, scope_id in scopes
            if scope_id
            for grant in grants
        )
        for domain in normalized_domains
    ):
        return "mixed"
    return None


def _upsert_domain_grant(scope: str, scope_id: str, domain: str, by_user_id: int) -> None:
    if not scope_id:
        return
    normalized = _normalize_domain(domain)
    if not normalized:
        return

    grants = _iter_domain_grants()
    for grant in grants:
        if _match_domain_grant(grant, scope, scope_id, normalized):
            return

    grants.append(
        {
            "scope": scope,
            "scope_id": scope_id,
            "domain": normalized,
            "allow": True,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "by_user_id": by_user_id,
        }
    )
    _RUNTIME_STORE["domain_grants"] = grants
    _save_store()


def _create_pending(payload: dict[str, Any]) -> str:
    pending_id = f"p{datetime.now().strftime('%m%d%H%M%S')}{secrets.token_hex(2)}"
    pending = _RUNTIME_STORE.get("pending") or {}
    pending[pending_id] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        **payload,
    }
    _RUNTIME_STORE["pending"] = pending
    _save_store()
    return pending_id


def _pop_pending(pending_id: str) -> dict[str, Any] | None:
    pending = _RUNTIME_STORE.get("pending") or {}
    if pending_id not in pending:
        return None
    item = pending.pop(pending_id)
    _RUNTIME_STORE["pending"] = pending
    _save_store()
    return item


def _format_two_stage_receipt(result_text: str, process_detail: str) -> str:
    return f"1,结果{result_text}\n2,过程详细{process_detail}"


def _default_screenshot_dirs()-> list[str]:
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
            "/agent - Show current agent",
            "/agent <name> - Set current agent",
            "/agent clear - Clear current agent",
            "",
            "Default behavior:",
            "- A normal message continues the current session.",
            "- If no current session exists, the first normal message creates one.",
            "- High-risk requests require Telegram approval by default.",
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


async def _send_telegram_message_with_keyboard(chat_id: int, text: str, inline_keyboard: list[list[dict[str, str]]]) -> None:
    if not TELEGRAM_API:
        return

    payload = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": {"inline_keyboard": inline_keyboard},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)
        if resp.status_code >= 400:
            _safe_log(f"[telegram send keyboard error] {resp.status_code} {resp.text}")


async def _answer_callback_query(callback_query_id: str, text: str) -> None:
    if not TELEGRAM_API:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        payload = {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": False,
        }
        resp = await client.post(f"{TELEGRAM_API}/answerCallbackQuery", json=payload)
        if resp.status_code >= 400:
            _safe_log(f"[telegram answerCallbackQuery error] {resp.status_code} {resp.text}")


def _build_approval_keyboard(pending_id: str, has_agent_scope: bool) -> list[list[dict[str, str]]]:
    keyboard: list[list[dict[str, str]]] = [
        [
            {"text": "✅ 仅本次", "callback_data": f"ap:{pending_id}:once"},
            {"text": "🔁 本对话允许同类", "callback_data": f"ap:{pending_id}:conversation"},
        ],
        [
            {"text": "📁 本项目允许同类", "callback_data": f"ap:{pending_id}:project"},
        ],
    ]
    if has_agent_scope:
        keyboard.append([
            {"text": "🤖 本Agent允许同类", "callback_data": f"ap:{pending_id}:agent"},
        ])
    keyboard.append([
        {"text": "❌ 拒绝", "callback_data": f"ap:{pending_id}:deny"},
    ])
    return keyboard


def _render_approval_prompt(
    pending_id: str,
    risk_kind: str,
    reason: str,
    prompt: str,
    domains: list[str] | None = None,
    planned_actions: list[dict[str, Any]] | None = None,
) -> str:
    compact_prompt = (prompt or "").strip().replace("\n", " ")
    if len(compact_prompt) > 200:
        compact_prompt = compact_prompt[:200] + "..."
    domain_summary = ""
    if domains:
        shown = ", ".join(domains[:5])
        extra = "" if len(domains) <= 5 else f" (+{len(domains) - 5})"
        domain_summary = f"\n域名摘要: {shown}{extra}"
    action_summary = ""
    if planned_actions:
        snippets: list[str] = []
        for action in planned_actions[:3]:
            action_type = str(action.get("type") or "other")
            summary = str(action.get("summary") or "").strip()
            if summary:
                snippets.append(f"- {action_type}: {summary}")
        if snippets:
            action_summary = "\n动作摘要:\n" + "\n".join(snippets)
    return (
        "高风险操作默认拒绝，需审批。\n"
        f"审批单: {pending_id}\n"
        f"风险类型: {risk_kind}\n"
        f"命中规则: {reason}\n"
        f"{domain_summary}"
        f"{action_summary}\n"
        f"请求摘要: {compact_prompt}"
    )


async def _execute_copilot(
    user_id: int,
    chat_id: int,
    copilot_prompt: str,
    current_session: str | None,
    agent_name: str | None,
    evidence_requested: bool,
    approval_source: str,
) -> None:
    copilot_cmd = _resolve_copilot_command()
    if not copilot_cmd:
        reply_text = (
            "Copilot CLI was not found. Ensure it is installed, or set in .env:\n"
            "COPILOT_COMMAND=C:\\path\\to\\copilot.ps1"
        )
        await _send_telegram_message(chat_id, reply_text)
        return

    sessions_before: set[str] = set(_list_sessions(limit=200))
    if evidence_requested:
        copilot_prompt = (
            f"{copilot_prompt}\n\n"
            f"截图证据请保存到目录: {EVIDENCE_SCREENSHOT_DIR}"
        )
    copilot_args = ["-p", copilot_prompt, "--allow-all-tools"]
    if agent_name:
        copilot_args.extend(["--agent", agent_name])
    if current_session:
        copilot_args = ["--resume", current_session] + copilot_args

    try:
        started_at = datetime.now().timestamp()
        result = subprocess.run(
            copilot_cmd + copilot_args,
            capture_output=True,
            text=False,
            env=os.environ.copy(),
            timeout=COPILOT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        detail = f"status=timeout; timeout={COPILOT_TIMEOUT_SECONDS}s; approval={approval_source}; session={current_session or 'new'}; agent={agent_name or '-'}"
        await _send_telegram_message(chat_id, _format_two_stage_receipt("执行超时", detail))
        _append_audit("execution_timeout", {
            "user_id": user_id,
            "chat_id": chat_id,
            "approval_source": approval_source,
            "session": current_session,
            "agent": agent_name,
        })
        return
    except Exception as exc:
        detail = f"status=launch_error; approval={approval_source}; session={current_session or 'new'}; agent={agent_name or '-'}"
        await _send_telegram_message(chat_id, _format_two_stage_receipt(f"启动失败: {exc}", detail))
        _append_audit("execution_launch_error", {
            "user_id": user_id,
            "chat_id": chat_id,
            "approval_source": approval_source,
            "session": current_session,
            "agent": agent_name,
            "error": str(exc),
        })
        return

    stdout = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()

    _safe_log(f"[copilot stdout] {stdout}")
    if result.returncode != 0:
        _safe_log(f"[copilot stderr] {stderr}")

    if result.returncode == 0 and stdout:
        result_text = stdout
    elif stderr:
        result_text = f"Copilot command failed: {stderr}"
    else:
        result_text = "Copilot returned no output."

    latest_sessions = _list_sessions(limit=1)
    if current_session:
        USER_ACTIVE_SESSION[user_id] = current_session
    else:
        sessions_after = _list_sessions(limit=200)
        new_sessions = [s for s in sessions_after if s not in sessions_before]
        if new_sessions:
            USER_ACTIVE_SESSION[user_id] = new_sessions[0]
        elif latest_sessions:
            USER_ACTIVE_SESSION[user_id] = latest_sessions[0]
    _save_user_sessions(USER_ACTIVE_SESSION)

    detail = (
        f"status=done; return_code={result.returncode}; approval={approval_source}; "
        f"session={USER_ACTIVE_SESSION.get(user_id) or 'new'}; agent={agent_name or '-'}"
    )
    await _send_telegram_message(chat_id, _format_two_stage_receipt(result_text, detail))

    _append_audit("execution", {
        "user_id": user_id,
        "chat_id": chat_id,
        "approval_source": approval_source,
        "return_code": result.returncode,
        "session": USER_ACTIVE_SESSION.get(user_id),
        "agent": agent_name,
    })

    if evidence_requested:
        try:
            latest_screenshot = _find_latest_screenshot(min_mtime=started_at)
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


def _is_explicit_evidence_request(text: str) -> bool:
    candidate = (text or "").strip()
    if not candidate:
        return False
    return any(pattern.search(candidate) for pattern in EVIDENCE_TRIGGER_PATTERNS)


def _find_latest_screenshot(min_mtime: float | None = None) -> Path | None:
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
            if min_mtime is not None and mtime < min_mtime:
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

        callback_query = data.get("callback_query")
        if callback_query:
            callback_id = callback_query.get("id")
            callback_data = callback_query.get("data") or ""
            callback_user_id = callback_query.get("from", {}).get("id")
            callback_chat_id = (callback_query.get("message") or {}).get("chat", {}).get("id")

            if not callback_id:
                return {"ok": True}

            if ALLOWED_USER_IDS and callback_user_id not in ALLOWED_USER_IDS:
                await _answer_callback_query(callback_id, "Not allowed")
                return {"ok": True}

            if not callback_data.startswith("ap:"):
                await _answer_callback_query(callback_id, "Unknown action")
                return {"ok": True}

            parts = callback_data.split(":", maxsplit=2)
            if len(parts) != 3:
                await _answer_callback_query(callback_id, "Malformed action")
                return {"ok": True}

            pending_id = parts[1]
            action = parts[2]
            valid_actions = {"once", "conversation", "project", "agent", "deny"}
            if action not in valid_actions:
                await _answer_callback_query(callback_id, "Unknown action")
                return {"ok": True}

            pending_map = _RUNTIME_STORE.get("pending") or {}
            pending = pending_map.get(pending_id)
            if not pending:
                await _answer_callback_query(callback_id, "审批单不存在或已处理")
                return {"ok": True}

            if int(pending.get("user_id", 0) or 0) != int(callback_user_id or 0):
                await _answer_callback_query(callback_id, "只能由原请求用户审批")
                return {"ok": True}

            pending_payload = pending
            risk_kind = str(pending_payload.get("risk_kind") or "general")
            risk_kinds_raw = pending_payload.get("risk_kinds") or []
            risk_kinds = [str(item).strip() for item in risk_kinds_raw if str(item).strip()]
            if not risk_kinds and risk_kind != "general":
                risk_kinds = [risk_kind]
            agent_name = pending_payload.get("agent_name")
            current_session = pending_payload.get("current_session")

            if action == "deny":
                _pop_pending(pending_id)
                await _answer_callback_query(callback_id, "已拒绝（仅本次）")
                if callback_chat_id:
                    await _send_telegram_message(callback_chat_id, "已拒绝本次高风险操作。")
                _append_audit("approval_deny_once", {
                    "pending_id": pending_id,
                    "user_id": callback_user_id,
                    "risk_kind": risk_kind,
                    "risk_kinds": risk_kinds,
                })
                return {"ok": True}

            if action in {"conversation", "project", "agent"}:
                if action == "conversation":
                    scope_id = current_session or "new-session"
                elif action == "project":
                    scope_id = PROJECT_SCOPE_KEY
                else:
                    if not agent_name:
                        await _answer_callback_query(callback_id, "当前请求无 agent 上下文")
                        return {"ok": True}
                    scope_id = agent_name
                for item in risk_kinds:
                    _upsert_allow_grant(action, scope_id, item, int(callback_user_id))
                for domain in pending_payload.get("domains") or []:
                    _upsert_domain_grant(action, scope_id, str(domain), int(callback_user_id))
                _append_audit("approval_grant", {
                    "pending_id": pending_id,
                    "user_id": callback_user_id,
                    "scope": action,
                    "scope_id": scope_id,
                    "risk_kind": risk_kind,
                    "risk_kinds": risk_kinds,
                    "domains": pending_payload.get("domains") or [],
                })

            pending_payload = _pop_pending(pending_id)
            if not pending_payload:
                await _answer_callback_query(callback_id, "审批单已过期")
                return {"ok": True}

            await _answer_callback_query(callback_id, "已批准，开始执行")
            await _execute_copilot(
                user_id=int(pending_payload["user_id"]),
                chat_id=int(pending_payload["chat_id"]),
                copilot_prompt=str(pending_payload["copilot_prompt"]),
                current_session=current_session,
                agent_name=agent_name,
                evidence_requested=bool(pending_payload.get("evidence_requested", False)),
                approval_source=f"callback:{action}",
            )
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

        if normalized_text.startswith("/agent"):
            parts = normalized_text.split(maxsplit=1)
            if len(parts) == 1:
                current_agent = USER_ACTIVE_AGENT.get(user_id)
                if chat_id:
                    await _send_telegram_message(chat_id, f"Current agent: {current_agent or '-'}")
                return {"ok": True}

            raw_agent = parts[1].strip()
            if raw_agent.lower() in {"clear", "none", "off"}:
                USER_ACTIVE_AGENT[user_id] = None
                _save_user_agents(USER_ACTIVE_AGENT)
                if chat_id:
                    await _send_telegram_message(chat_id, "Agent cleared for this user.")
                return {"ok": True}

            USER_ACTIVE_AGENT[user_id] = raw_agent
            _save_user_agents(USER_ACTIVE_AGENT)
            if chat_id:
                await _send_telegram_message(chat_id, f"Agent set: {raw_agent}")
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

        current_session = USER_ACTIVE_SESSION.get(user_id)
        agent_name = USER_ACTIVE_AGENT.get(user_id)
        evidence_requested_fallback = _is_explicit_evidence_request(text)

        local_image_path = await _download_telegram_image(user_id, message)
        copilot_prompt = text
        if local_image_path:
            if copilot_prompt.strip():
                copilot_prompt = f"{copilot_prompt}\n\n[Telegram image path]\n{local_image_path}"
            else:
                copilot_prompt = f"Analyze the attached Telegram image at path: {local_image_path}"

        extracted_domains = _extract_domains_from_text(copilot_prompt)
        plan_result: dict[str, Any] = {
            "actions": [],
            "domains": [],
            "needs_evidence": False,
            "confidence": 0.0,
            "parse_ok": False,
        }
        if PLAN_FIRST_MODE:
            copilot_cmd = _resolve_copilot_command()
            if copilot_cmd:
                plan_result = _plan_actions_with_copilot(copilot_cmd, copilot_prompt, current_session, agent_name)
            else:
                _safe_log("[plan] skipped: copilot command unavailable")
        else:
            heuristic_actions: list[dict[str, str]] = []
            heuristic_risky, heuristic_kind, _ = _classify_risk(copilot_prompt)
            if heuristic_risky:
                heuristic_actions.append({"type": heuristic_kind, "summary": "heuristic risk match"})
            if extracted_domains:
                heuristic_actions.append({"type": "network", "summary": "detected URL/domain in request"})
            plan_result = {
                "actions": heuristic_actions,
                "domains": extracted_domains,
                "needs_evidence": evidence_requested_fallback,
                "confidence": 1.0,
                "parse_ok": True,
            }

        plan_parse_ok = bool(plan_result.get("parse_ok"))
        plan_confidence = float(plan_result.get("confidence") or 0.0)

        planned_actions_raw = plan_result.get("actions") or []
        planned_actions: list[dict[str, Any]] = []
        for item in planned_actions_raw:
            if not isinstance(item, dict):
                continue
            action_type = str(item.get("type") or "other").strip().lower()
            summary = str(item.get("summary") or "").strip()
            planned_actions.append({"type": action_type, "summary": summary})

        high_risk_types = ["destructive", "repo_write", "secret"]
        risk_kinds = sorted({
            action.get("type")
            for action in planned_actions
            if action.get("type") in high_risk_types
        })

        risk_scopes: dict[str, str | None] = {
            kind: _resolve_allow_scope(user_id, agent_name, current_session, kind)
            for kind in risk_kinds
        }
        missing_risk_kinds = [kind for kind, scope in risk_scopes.items() if not scope]

        planned_domains: list[str] = []
        seen_domains: set[str] = set()
        for raw_domain in plan_result.get("domains") or []:
            normalized = _normalize_domain(str(raw_domain))
            if normalized and normalized not in seen_domains:
                seen_domains.add(normalized)
                planned_domains.append(normalized)
        for domain in extracted_domains:
            normalized = _normalize_domain(domain)
            if normalized and normalized not in seen_domains:
                seen_domains.add(normalized)
                planned_domains.append(normalized)

        has_network_action = any(action.get("type") == "network" for action in planned_actions) or bool(planned_domains)
        network_allow_scope = _resolve_domain_allow_scope(user_id, agent_name, current_session, planned_domains) if has_network_action else None
        unauthorized_domains: list[str] = []
        if has_network_action:
            scopes_for_check = [
                ("user", str(user_id)),
                ("agent", agent_name or ""),
                ("project", PROJECT_SCOPE_KEY),
                ("conversation", current_session or "new-session"),
            ]
            all_domain_grants = _iter_domain_grants()
            for domain in planned_domains:
                if not any(
                    _match_domain_grant(grant, scope, scope_id, domain)
                    for scope, scope_id in scopes_for_check
                    if scope_id
                    for grant in all_domain_grants
                ):
                    unauthorized_domains.append(domain)

        plan_fail_closed = PLAN_FIRST_MODE and (not plan_parse_ok or plan_confidence < PLAN_CONFIDENCE_THRESHOLD)
        requires_approval = plan_fail_closed or bool(missing_risk_kinds) or (has_network_action and bool(unauthorized_domains))

        if EVIDENCE_LLM_ENABLED:
            evidence_requested = bool(plan_result.get("needs_evidence", False))
        else:
            evidence_requested = evidence_requested_fallback

        if requires_approval:
            effective_risk_kind = "plan"
            reason_parts: list[str] = []
            if plan_fail_closed:
                if not plan_parse_ok:
                    reason_parts.append("action plan 解析失败（fail-closed）")
                else:
                    reason_parts.append(f"action plan 置信度过低: {plan_confidence:.2f} < {PLAN_CONFIDENCE_THRESHOLD:.2f}")
            if missing_risk_kinds:
                effective_risk_kind = missing_risk_kinds[0]
                reason_parts.append(f"高风险动作未授权: {', '.join(missing_risk_kinds)}")
            if has_network_action and unauthorized_domains:
                effective_risk_kind = "network"
                shown_domains = unauthorized_domains
                domain_text = ", ".join(shown_domains[:5])
                more = "" if len(shown_domains) <= 5 else f" (+{len(shown_domains) - 5})"
                reason_parts.append(f"未授权网络域名: {domain_text}{more}" if domain_text else "未授权网络访问")
            effective_reason = "；".join(reason_parts) if reason_parts else "需审批"
            pending_id = _create_pending(
                {
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "copilot_prompt": copilot_prompt,
                    "current_session": current_session,
                    "agent_name": agent_name,
                    "evidence_requested": evidence_requested,
                    "risk_kind": effective_risk_kind,
                    "risk_kinds": missing_risk_kinds,
                    "domains": unauthorized_domains,
                    "planned_actions": planned_actions,
                    "plan_confidence": plan_confidence,
                }
            )
            _append_audit("approval_required", {
                "pending_id": pending_id,
                "user_id": user_id,
                "chat_id": chat_id,
                "risk_kind": effective_risk_kind,
                "risk_kinds": missing_risk_kinds,
                "reason": effective_reason,
                "session": current_session,
                "agent": agent_name,
                "domains": unauthorized_domains,
                "plan_confidence": plan_confidence,
            })
            if chat_id:
                await _send_telegram_message_with_keyboard(
                    chat_id,
                    _render_approval_prompt(
                        pending_id,
                        effective_risk_kind,
                        effective_reason,
                        copilot_prompt,
                        unauthorized_domains,
                        planned_actions,
                    ),
                    _build_approval_keyboard(pending_id, has_agent_scope=bool(agent_name)),
                )
            return {"ok": True}

        allow_scope = None
        if risk_kinds:
            allow_scope = ",".join(sorted({scope for scope in risk_scopes.values() if scope}))

        if chat_id:
            await _execute_copilot(
                user_id=user_id,
                chat_id=chat_id,
                copilot_prompt=copilot_prompt,
                current_session=current_session,
                agent_name=agent_name,
                evidence_requested=evidence_requested,
                approval_source=(
                    f"auto:risk:{allow_scope}"
                    if allow_scope
                    else (f"auto:network:{network_allow_scope}" if network_allow_scope else "auto:low-risk")
                ),
            )

        return {"ok": True}

    return _app


app = create_app()
