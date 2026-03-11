from datetime import datetime
from pathlib import Path

import httpx

from core.runtime_state import (
    TELEGRAM_API,
    TELEGRAM_FILE_API,
    TELEGRAM_IMAGE_EXTENSIONS,
    TELEGRAM_IMAGE_MAX_BYTES,
    TELEGRAM_MEDIA_DIR,
    _safe_log,
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


async def _send_telegram_photo(chat_id: int, file_path: Path) -> bool:
    if not TELEGRAM_API or not file_path.exists() or not file_path.is_file():
        return False

    async with httpx.AsyncClient(timeout=60) as client:
        with file_path.open("rb") as file_obj:
            resp = await client.post(
                f"{TELEGRAM_API}/sendDocument",
                data={"chat_id": str(chat_id)},
                files={"document": (file_path.name, file_obj, "application/octet-stream")},
            )
        if resp.status_code < 400:
            return True

        _safe_log(f"[telegram sendDocument error] {resp.status_code} {resp.text}")
        with file_path.open("rb") as file_obj:
            fallback_resp = await client.post(
                f"{TELEGRAM_API}/sendPhoto",
                data={"chat_id": str(chat_id)},
                files={"photo": (file_path.name, file_obj, "application/octet-stream")},
            )
        if fallback_resp.status_code < 400:
            return True

        _safe_log(f"[telegram sendPhoto error] {fallback_resp.status_code} {fallback_resp.text}")
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