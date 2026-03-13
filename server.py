from fastapi import FastAPI, Request
from dotenv import load_dotenv
import subprocess
import os
import shutil
import re
import yaml
import json
from collections import deque
from pathlib import Path
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse

from core import approval_flow

from core.policy_engine import _decide_plan_policy, _decide_shadow_enforcement, _recommend_shadow_strategy
from core.pipeline_context import build_message_context
from core.telegram_io import (
    _answer_callback_query,
    _download_telegram_image,
    _send_telegram_message,
    _send_telegram_message_with_keyboard,
    _send_telegram_photo,
)

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


PLAN_CONFIDENCE_THRESHOLD = _env_float("PLAN_CONFIDENCE_THRESHOLD", 0.7)
SHADOW_ENFORCEMENT_ENABLED = _env_flag("SHADOW_ENFORCEMENT_ENABLED", False)
_raw_shadow_enforcement_scope = (os.getenv("SHADOW_ENFORCEMENT_SCOPE") or "deny_only").strip().lower()
SHADOW_ENFORCEMENT_SCOPE: Literal["deny_only", "deny_and_challenge"] = (
    _raw_shadow_enforcement_scope
    if _raw_shadow_enforcement_scope in {"deny_only", "deny_and_challenge"}
    else "deny_only"
)

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


def _is_playwright_persistent_launch_error(stderr: str) -> bool:
    normalized = (stderr or "").lower()
    return (
        "browsertype.launchpersistentcontext" in normalized
        and "failed to launch the browser process" in normalized
    )


_USER_SESSION_PERSIST_FILE = Path(__file__).parent / "user_sessions.json"
_STORE_FILE = Path(__file__).parent / "approval_store.json"
_AUDIT_FILE = Path(__file__).parent / "audit_log.jsonl"


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


def _extract_frontmatter_name(agent_file: Path) -> str | None:
    try:
        text = agent_file.read_text(encoding="utf-8")
    except Exception:
        return None

    if not text.startswith("---"):
        return None

    parts = text.split("---", 2)
    if len(parts) < 3:
        return None

    try:
        frontmatter = yaml.safe_load(parts[1])
    except Exception:
        return None

    if not isinstance(frontmatter, dict):
        return None
    name = str(frontmatter.get("name") or "").strip()
    return name or None


def _discover_agent_names() -> list[str]:
    start = Path.cwd().resolve()
    roots: list[Path] = []
    cur = start
    while True:
        roots.append(cur)
        if cur.parent == cur:
            break
        cur = cur.parent

    names: set[str] = set()
    for root in roots:
        agents_dir = root / ".github" / "agents"
        if not agents_dir.exists() or not agents_dir.is_dir():
            continue
        for file_path in sorted(agents_dir.glob("*.agent.md")):
            name = _extract_frontmatter_name(file_path)
            if name:
                names.add(name)

    return sorted(names)


def _discover_models_from_copilot_package() -> tuple[list[dict[str, Any]], str | None]:
    candidates: list[Path] = []
    appdata = os.getenv("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "node_modules" / "@github" / "copilot" / "sdk" / "index.d.ts")

    userprofile = os.getenv("USERPROFILE")
    if userprofile:
        candidates.append(
            Path(userprofile)
            / "AppData"
            / "Roaming"
            / "npm"
            / "node_modules"
            / "@github"
            / "copilot"
            / "sdk"
            / "index.d.ts"
        )

    dts_file = next((path for path in candidates if path.exists() and path.is_file()), None)
    if not dts_file:
        return [], "copilot SDK typings not found"

    try:
        text = dts_file.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return [], f"failed to read SDK typings: {exc}"

    enum_match = re.search(r"SUPPORTED_MODELS:\s*readonly\s*\[(.*?)\];", text, re.DOTALL)
    if not enum_match:
        return [], "SUPPORTED_MODELS not found in SDK typings"

    raw_models = re.findall(r'"([^"]+)"', enum_match.group(1))
    if not raw_models:
        return [], "no models found in SUPPORTED_MODELS"

    seen: set[str] = set()
    models: list[dict[str, Any]] = []
    for model_id in raw_models:
        if model_id in seen:
            continue
        seen.add(model_id)

        lowered = model_id.lower()
        if "max" in lowered:
            multiplier = 3
        elif "mini" in lowered:
            multiplier = 0
        else:
            multiplier = 1

        models.append({"id": model_id, "multiplier": multiplier})

    return models, None


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
    model_id: str | None,
) -> dict[str, Any]:
    plan_prompt = _build_plan_prompt(user_prompt)
    copilot_args = ["-p", plan_prompt]
    if agent_name:
        copilot_args.extend(["--agent", agent_name])
    if model_id:
        copilot_args.extend(["--model", model_id])
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


def _approval_flow_deps() -> approval_flow.ApprovalFlowDeps:
    return approval_flow.ApprovalFlowDeps(
        runtime_store=_RUNTIME_STORE,
        save_store=_save_store,
        append_audit=_append_audit,
        answer_callback_query=_answer_callback_query,
        send_telegram_message=_send_telegram_message,
        execute_copilot=_execute_copilot,
        normalize_domain=_normalize_domain,
        project_scope_key=PROJECT_SCOPE_KEY,
        allowed_user_ids=ALLOWED_USER_IDS,
    )


def _iter_allow_grants() -> list[dict[str, Any]]:
    return approval_flow.iter_allow_grants(_RUNTIME_STORE)


def _match_grant(grant: dict[str, Any], scope: str, scope_id: str, risk_kind: str) -> bool:
    return approval_flow.match_grant(grant, scope, scope_id, risk_kind)


def _build_authorization_scopes(
    user_id: int,
    agent_name: str | None,
    current_session: str | None,
) -> tuple[tuple[str, str], ...]:
    return approval_flow.build_authorization_scopes(
        user_id,
        agent_name,
        current_session,
        PROJECT_SCOPE_KEY,
    )


def _resolve_allow_scope(user_id: int, agent_name: str | None, current_session: str | None, risk_kind: str) -> str | None:
    return approval_flow.resolve_allow_scope(
        user_id,
        agent_name,
        current_session,
        risk_kind,
        _RUNTIME_STORE,
        PROJECT_SCOPE_KEY,
    )


def _upsert_allow_grant(scope: str, scope_id: str, risk_kind: str, by_user_id: int) -> None:
    approval_flow.upsert_allow_grant(scope, scope_id, risk_kind, by_user_id, _RUNTIME_STORE, _save_store)


def _iter_domain_grants() -> list[dict[str, Any]]:
    return approval_flow.iter_domain_grants(_RUNTIME_STORE, _normalize_domain)


def _match_domain_grant(grant: dict[str, Any], scope: str, scope_id: str, domain: str) -> bool:
    return approval_flow.match_domain_grant(grant, scope, scope_id, domain, _normalize_domain)


def _resolve_domain_allow_scope(
    user_id: int,
    agent_name: str | None,
    current_session: str | None,
    domains: list[str],
) -> str | None:
    return approval_flow.resolve_domain_allow_scope(
        user_id,
        agent_name,
        current_session,
        domains,
        _RUNTIME_STORE,
        _normalize_domain,
        PROJECT_SCOPE_KEY,
    )


def _upsert_domain_grant(scope: str, scope_id: str, domain: str, by_user_id: int) -> None:
    approval_flow.upsert_domain_grant(scope, scope_id, domain, by_user_id, _RUNTIME_STORE, _save_store, _normalize_domain)


def _create_pending(payload: dict[str, Any]) -> str:
    return approval_flow.create_pending(payload, _RUNTIME_STORE, _save_store)


def _pop_pending(pending_id: str) -> dict[str, Any] | None:
    return approval_flow.pop_pending(pending_id, _RUNTIME_STORE, _save_store)


def _format_two_stage_receipt(result_text: str, process_detail: str) -> str:
    return f"1,Result: {result_text}\n2,Process details: {process_detail}"


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
    lines.append("\nUsage: /session <number>  e.g. /session 2")
    return "\n".join(lines)


def _render_agents(current_agent: str | None) -> str:
    discovered = _discover_agent_names()
    options: list[str | None] = [None] + discovered

    lines = ["Available agents:"]
    for idx, agent in enumerate(options, start=1):
        label = "none" if agent is None else agent
        selected = (agent is None and not current_agent) or (agent is not None and agent == current_agent)
        marker = " ✅" if selected else ""
        lines.append(f"{idx}. `{label}`{marker}")

    lines.append("\nUsage: /agent <number>  e.g. /agent 2")
    return "\n".join(lines)


def _render_models(current_model: str | None, models: list[dict[str, Any]]) -> str:
    lines = ["Available models:"]
    for idx, model in enumerate(models, start=1):
        model_id = str(model.get("id") or "")
        multiplier = int(model.get("multiplier") or 1)
        selected = current_model == model_id
        marker = " ✅" if selected else ""
        lines.append(f"{idx}. `{model_id}` (x{multiplier}){marker}")

    lines.append("\nUsage: /model <number>  e.g. /model 3")
    return "\n".join(lines)


def _render_help() -> str:
    return "\n".join(
        [
            "Commands:",
            "/help - Show help",
            "/new - Start a fresh session (next normal message creates a new session)",
            "/sessions - List recent sessions",
            "/session <id> - Switch to a session by list number",
            "/agents - List available agents",
            "/agent <id> - Set agent by list number (1 = none)",
            "/models - List available models",
            "/model <id> - Set model by list number",
            "",
            "Default behavior:",
            "- A normal message continues the current session.",
            "- If no current session exists, the first normal message creates one.",
            "- High-risk requests require Telegram approval by default.",
        ]
    )


def _build_approval_keyboard(pending_id: str, has_agent_scope: bool) -> list[list[dict[str, str]]]:
    return approval_flow.build_approval_keyboard(pending_id, has_agent_scope)


def _render_approval_prompt(
    pending_id: str,
    risk_kind: str,
    reason: str,
    prompt: str,
    domains: list[str] | None = None,
    planned_actions: list[dict[str, Any]] | None = None,
) -> str:
    return approval_flow.render_approval_prompt(
        pending_id,
        risk_kind,
        reason,
        prompt,
        domains,
        planned_actions,
    )


async def _handle_callback_approval(callback_query: dict[str, Any] | None) -> bool:
    return await approval_flow.handle_callback_approval(callback_query, _approval_flow_deps())


async def _handle_command_message(user_id: int, chat_id: int | None, normalized_text: str) -> bool:
    if normalized_text == "/new":
        USER_ACTIVE_SESSION[user_id] = None
        _save_user_sessions(USER_ACTIVE_SESSION)
        if chat_id:
            await _send_telegram_message(chat_id, "Switched to new-session mode. Your next normal message will create a new session.")
        return True

    if normalized_text == "/help":
        if chat_id:
            await _send_telegram_message(chat_id, _render_help())
        return True

    if normalized_text == "/sessions":
        current_session = USER_ACTIVE_SESSION.get(user_id)
        if chat_id:
            await _send_telegram_message(chat_id, _render_sessions(current_session, limit=SESSION_LIST_LIMIT))
        return True

    if normalized_text.startswith("/session"):
        parts = normalized_text.split(maxsplit=1)
        if len(parts) < 2:
            if chat_id:
                await _send_telegram_message(chat_id, "Usage: /session <number>")
            return True

        raw_number = parts[1].strip().lstrip("#")
        if not raw_number.isdigit():
            if chat_id:
                await _send_telegram_message(chat_id, "Invalid session id. Use /sessions and pick a number.")
            return True

        idx = int(raw_number) - 1
        all_sessions = _list_sessions(limit=200)
        if idx < 0 or idx >= len(all_sessions):
            if chat_id:
                await _send_telegram_message(chat_id, f"Invalid session id: {raw_number}. Run /sessions first.")
            return True

        resolved = all_sessions[idx]
        USER_ACTIVE_SESSION[user_id] = resolved
        _save_user_sessions(USER_ACTIVE_SESSION)
        if chat_id:
            await _send_telegram_message(chat_id, f"Switched to session: {resolved}")
        return True

    if normalized_text == "/agents":
        current_agent = USER_ACTIVE_AGENT.get(user_id)
        if chat_id:
            await _send_telegram_message(chat_id, _render_agents(current_agent))
        return True

    if normalized_text.startswith("/agent"):
        parts = normalized_text.split(maxsplit=1)
        if len(parts) == 1:
            current_agent = USER_ACTIVE_AGENT.get(user_id)
            if chat_id:
                await _send_telegram_message(chat_id, f"Current agent: {current_agent or '-'}")
            return True

        raw_number = parts[1].strip().lstrip("#")
        if not raw_number.isdigit():
            if chat_id:
                await _send_telegram_message(chat_id, "Invalid agent id. Use /agents and pick a number.")
            return True

        selected_idx = int(raw_number)
        discovered = _discover_agent_names()
        options: list[str | None] = [None] + discovered
        if selected_idx < 1 or selected_idx > len(options):
            if chat_id:
                await _send_telegram_message(chat_id, f"Invalid agent id: {selected_idx}. Run /agents first.")
            return True

        selected_agent = options[selected_idx - 1]
        if selected_agent is None:
            USER_ACTIVE_AGENT[user_id] = None
            _save_user_agents(USER_ACTIVE_AGENT)
            if chat_id:
                await _send_telegram_message(chat_id, "Agent cleared for this user.")
            return True

        USER_ACTIVE_AGENT[user_id] = selected_agent
        _save_user_agents(USER_ACTIVE_AGENT)
        if chat_id:
            await _send_telegram_message(chat_id, f"Agent set: {selected_agent}")
        return True

    if normalized_text == "/models":
        models, model_error = _discover_models_from_copilot_package()
        if chat_id:
            if not models:
                await _send_telegram_message(
                    chat_id,
                    f"No models discovered from installed Copilot package. ({model_error or 'unknown error'})",
                )
            else:
                current_model = USER_ACTIVE_MODEL.get(user_id)
                await _send_telegram_message(chat_id, _render_models(current_model, models))
        return True

    if normalized_text.startswith("/model"):
        parts = normalized_text.split(maxsplit=1)
        if len(parts) == 1:
            current_model = USER_ACTIVE_MODEL.get(user_id)
            if chat_id:
                await _send_telegram_message(chat_id, f"Current model: {current_model or '-'}")
            return True

        raw_number = parts[1].strip().lstrip("#")
        if not raw_number.isdigit():
            if chat_id:
                await _send_telegram_message(chat_id, "Invalid model id. Use /models and pick a number.")
            return True

        models, model_error = _discover_models_from_copilot_package()
        if not models:
            if chat_id:
                await _send_telegram_message(
                    chat_id,
                    f"No models discovered from installed Copilot package. ({model_error or 'unknown error'})",
                )
            return True

        selected_idx = int(raw_number)
        if selected_idx < 1 or selected_idx > len(models):
            if chat_id:
                await _send_telegram_message(chat_id, f"Invalid model id: {selected_idx}. Run /models first.")
            return True

        selected_model = str(models[selected_idx - 1].get("id") or "").strip()
        if not selected_model:
            if chat_id:
                await _send_telegram_message(chat_id, "Failed to resolve selected model id.")
            return True

        USER_ACTIVE_MODEL[user_id] = selected_model
        _save_user_models(USER_ACTIVE_MODEL)
        if chat_id:
            await _send_telegram_message(chat_id, f"Model set: {selected_model}")
        return True

    return False


async def _execute_copilot(
    user_id: int,
    chat_id: int,
    copilot_prompt: str,
    current_session: str | None,
    agent_name: str | None,
    model_id: str | None,
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
            f"Save screenshot evidence to: {EVIDENCE_SCREENSHOT_DIR}"
        )
    base_copilot_args = ["-p", copilot_prompt, "--allow-all-tools"]
    if agent_name:
        base_copilot_args.extend(["--agent", agent_name])
    if model_id:
        base_copilot_args.extend(["--model", model_id])
    copilot_args = list(base_copilot_args)
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
        detail = (
            f"status=timeout; timeout={COPILOT_TIMEOUT_SECONDS}s; approval={approval_source}; "
            f"session={current_session or 'new'}; agent={agent_name or '-'}; model={model_id or '-'}"
        )
        await _send_telegram_message(chat_id, _format_two_stage_receipt("Execution timed out", detail))
        _append_audit("execution_timeout", {
            "user_id": user_id,
            "chat_id": chat_id,
            "approval_source": approval_source,
            "session": current_session,
            "agent": agent_name,
            "model": model_id,
        })
        return
    except Exception as exc:
        detail = (
            f"status=launch_error; approval={approval_source}; session={current_session or 'new'}; "
            f"agent={agent_name or '-'}; model={model_id or '-'}"
        )
        await _send_telegram_message(chat_id, _format_two_stage_receipt(f"Launch failed: {exc}", detail))
        _append_audit("execution_launch_error", {
            "user_id": user_id,
            "chat_id": chat_id,
            "approval_source": approval_source,
            "session": current_session,
            "agent": agent_name,
            "model": model_id,
            "error": str(exc),
        })
        return

    stdout = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()

    if result.returncode != 0 and current_session and _is_playwright_persistent_launch_error(stderr):
        _safe_log(
            "[copilot retry] detected Playwright persistent launch error; retrying once without --resume"
        )
        _append_audit("execution_retry_fresh_session", {
            "user_id": user_id,
            "chat_id": chat_id,
            "approval_source": approval_source,
            "session": current_session,
            "agent": agent_name,
            "model": model_id,
            "reason": "playwright_launch_persistent_context_failed",
        })
        try:
            retry_result = subprocess.run(
                copilot_cmd + base_copilot_args,
                capture_output=True,
                text=False,
                env=os.environ.copy(),
                timeout=COPILOT_TIMEOUT_SECONDS,
            )
            retry_stdout = (retry_result.stdout or b"").decode("utf-8", errors="replace").strip()
            retry_stderr = (retry_result.stderr or b"").decode("utf-8", errors="replace").strip()
            if retry_result.returncode == 0:
                result = retry_result
                stdout = retry_stdout
                stderr = retry_stderr
        except Exception as retry_exc:
            _safe_log(f"[copilot retry] fresh-session retry failed before completion: {retry_exc}")

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
        f"session={USER_ACTIVE_SESSION.get(user_id) or 'new'}; agent={agent_name or '-'}; model={model_id or '-'}"
    )
    await _send_telegram_message(chat_id, _format_two_stage_receipt(result_text, detail))

    _append_audit("execution", {
        "user_id": user_id,
        "chat_id": chat_id,
        "approval_source": approval_source,
        "return_code": result.returncode,
        "session": USER_ACTIVE_SESSION.get(user_id),
        "agent": agent_name,
        "model": model_id,
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
        if await _handle_callback_approval(callback_query):
            return {"ok": True}

        message = data.get("message")
        if not message:
            return {"ok": True}

        context = build_message_context(message)
        user_id = context.user_id
        text = context.text

        _safe_log(f"[telegram] message from user_id={user_id} text='{context.safe_text}'")

        if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
            return {"ok": True}

        chat_id = context.chat_id
        normalized_text = context.normalized_text

        if await _handle_command_message(user_id, chat_id, normalized_text):
            return {"ok": True}

        current_session = USER_ACTIVE_SESSION.get(user_id)
        agent_name = USER_ACTIVE_AGENT.get(user_id)
        model_id = USER_ACTIVE_MODEL.get(user_id)

        local_image_path = await _download_telegram_image(user_id, context.message)
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
        copilot_cmd = _resolve_copilot_command()
        if copilot_cmd:
            plan_result = _plan_actions_with_copilot(copilot_cmd, copilot_prompt, current_session, agent_name, model_id)
        else:
            _safe_log("[plan] skipped: copilot command unavailable")

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
            scopes_for_check = _build_authorization_scopes(user_id, agent_name, current_session)
            all_domain_grants = _iter_domain_grants()
            for domain in planned_domains:
                if not any(
                    _match_domain_grant(grant, scope, scope_id, domain)
                    for scope, scope_id in scopes_for_check
                    if scope_id
                    for grant in all_domain_grants
                ):
                    unauthorized_domains.append(domain)

        decision = _decide_plan_policy(
            plan_parse_ok=plan_parse_ok,
            plan_confidence=plan_confidence,
            plan_confidence_threshold=PLAN_CONFIDENCE_THRESHOLD,
            missing_risk_kinds=missing_risk_kinds,
            has_network_action=has_network_action,
            unauthorized_domains=unauthorized_domains,
            risk_scopes=risk_scopes,
            network_allow_scope=network_allow_scope,
        )

        evidence_requested = bool(plan_result.get("needs_evidence", False))

        planned_action_types = sorted({action.get("type") for action in planned_actions if action.get("type")})
        execution_path = "approval_gate" if decision["requires_approval"] else "direct_execute"
        _append_audit("plan_policy_decision", {
            "user_id": user_id,
            "chat_id": chat_id,
            "session": current_session,
            "agent": agent_name,
            "model": model_id,
            "plan_parse_ok": plan_parse_ok,
            "plan_confidence": plan_confidence,
            "planned_action_types": planned_action_types,
            "planned_action_count": len(planned_actions),
            "planned_domains_count": len(planned_domains),
            "unauthorized_domains_count": len(unauthorized_domains),
            "missing_risk_kinds": missing_risk_kinds,
            "requires_approval": decision["requires_approval"],
            "plan_fail_closed": decision["plan_fail_closed"],
            "effective_risk_kind": decision["effective_risk_kind"],
            "effective_reason": decision["effective_reason"],
            "allow_scope": decision["allow_scope"],
            "network_allow_scope": network_allow_scope,
            "approval_source": decision["approval_source"],
            "evidence_requested": evidence_requested,
            "execution_path": execution_path,
        })

        shadow_recommendation = _recommend_shadow_strategy(
            plan_fail_closed=decision["plan_fail_closed"],
            has_network_action=has_network_action,
            unauthorized_domains=unauthorized_domains,
            missing_risk_kinds=missing_risk_kinds,
            planned_action_types=planned_action_types,
        )
        _append_audit("shadow_strategy_recommendation", {
            "user_id": user_id,
            "chat_id": chat_id,
            "session": current_session,
            "agent": agent_name,
            "model": model_id,
            "strategy": shadow_recommendation["strategy"],
            "reason_codes": shadow_recommendation["reason_codes"],
            "confidence": shadow_recommendation["confidence"],
            "summary": shadow_recommendation["summary"],
            "audit_only": True,
            "pre_enforcement_requires_approval": decision["requires_approval"],
            "pre_enforcement_execution_path": execution_path,
            "planned_action_types": planned_action_types,
            "planned_action_count": len(planned_actions),
            "missing_risk_kinds": missing_risk_kinds,
            "unauthorized_domains_count": len(unauthorized_domains),
            "plan_fail_closed": decision["plan_fail_closed"],
        })

        if SHADOW_ENFORCEMENT_ENABLED:
            shadow_enforcement_action = _decide_shadow_enforcement(
                enabled=SHADOW_ENFORCEMENT_ENABLED,
                scope=SHADOW_ENFORCEMENT_SCOPE,
                strategy=shadow_recommendation["strategy"],
            )
            pre_enforcement_requires_approval = decision["requires_approval"]
            pre_enforcement_execution_path = "approval_gate" if pre_enforcement_requires_approval else "direct_execute"

            if shadow_enforcement_action == "force_challenge":
                decision["requires_approval"] = True

            blocked = shadow_enforcement_action == "force_deny"
            post_enforcement_requires_approval = decision["requires_approval"]
            post_enforcement_execution_path = (
                "blocked"
                if blocked
                else ("approval_gate" if post_enforcement_requires_approval else "direct_execute")
            )

            _append_audit("shadow_enforcement_decision", {
                "user_id": user_id,
                "chat_id": chat_id,
                "session": current_session,
                "agent": agent_name,
                "model": model_id,
                "scope": SHADOW_ENFORCEMENT_SCOPE,
                "strategy": shadow_recommendation["strategy"],
                "action": shadow_enforcement_action,
                "pre_enforcement_requires_approval": pre_enforcement_requires_approval,
                "pre_enforcement_execution_path": pre_enforcement_execution_path,
                "post_enforcement_requires_approval": post_enforcement_requires_approval,
                "post_enforcement_execution_path": post_enforcement_execution_path,
                "blocked": blocked,
            })

            if blocked:
                _append_audit("shadow_enforcement_blocked", {
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "session": current_session,
                    "agent": agent_name,
                    "model": model_id,
                    "scope": SHADOW_ENFORCEMENT_SCOPE,
                    "strategy": shadow_recommendation["strategy"],
                    "reason_codes": shadow_recommendation["reason_codes"],
                    "planned_action_types": planned_action_types,
                    "unauthorized_domains_count": len(unauthorized_domains),
                    "missing_risk_kinds": missing_risk_kinds,
                })
                if chat_id:
                    await _send_telegram_message(chat_id, "Request blocked by policy: high-risk deny condition detected.")
                return {"ok": True}

        if decision["requires_approval"]:
            effective_risk_kind = decision["effective_risk_kind"]
            effective_reason = decision["effective_reason"]
            pending_id = _create_pending(
                {
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "copilot_prompt": copilot_prompt,
                    "current_session": current_session,
                    "agent_name": agent_name,
                    "model_id": model_id,
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
                "model": model_id,
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

        allow_scope = decision["allow_scope"]

        if chat_id:
            await _execute_copilot(
                user_id=user_id,
                chat_id=chat_id,
                copilot_prompt=copilot_prompt,
                current_session=current_session,
                agent_name=agent_name,
                model_id=model_id,
                evidence_requested=evidence_requested,
                approval_source=decision["approval_source"],
            )

        return {"ok": True}

    return _app


app = create_app()
