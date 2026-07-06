import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from twin import pushover

LOG_DIR = Path("./logs")
LOG_FILE = LOG_DIR / "knowledge_gaps.jsonl"

_lock = Lock()
_notified: set[str] = set()  # normalized questions already pinged this process


def _normalize(question: str) -> str:
    return " ".join(question.lower().split())


def record_gap(question: str, topic: str = "", visitor_name: str = "") -> bool:
    """Log an unanswered visitor question and ping Greg about it.

    The Pushover call runs on a daemon thread so a slow or failing notification
    can never stall the visitor's conversation. Repeat questions are logged but
    only notified once per app run. Returns whether a notification was sent."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "question": question,
        "topic": topic,
        "visitor_name": visitor_name,
    }
    is_new = _normalize(question) not in _notified
    entry["duplicate"] = not is_new
    with _lock:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    if is_new:
        _notified.add(_normalize(question))
        threading.Thread(
            target=_notify, args=(question, topic, visitor_name), daemon=True
        ).start()
    return is_new


def _notify(question: str, topic: str, visitor_name: str) -> None:
    message = f'A visitor asked: "{question}"'
    if visitor_name:
        message += f" (asked by {visitor_name})"
    message += " — not covered by the knowledge base. Add the answer to make it available."
    title = f"Knowledge gap: {topic}" if topic else "Knowledge gap"
    try:
        pushover.send_notification(message, title=title)
    except Exception:
        pass  # the gap is already in the JSONL log
