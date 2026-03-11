from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MessageContext:
    message: dict[str, Any]
    user_id: int | None
    text: str
    chat_id: int | None
    normalized_text: str
    safe_text: str


def build_message_context(message: dict[str, Any]) -> MessageContext:
    text = message.get("text") or message.get("caption", "")
    return MessageContext(
        message=message,
        user_id=message.get("from", {}).get("id"),
        text=text,
        chat_id=message.get("chat", {}).get("id"),
        normalized_text=text.strip(),
        safe_text=text.encode("ascii", errors="backslashreplace").decode("ascii"),
    )