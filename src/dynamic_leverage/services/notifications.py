from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class TelegramNotifier:
    """Small process-local deduplicator for operational Telegram alerts.

    The dashboard and scheduler are single-process workers. Keeping the last
    state in memory prevents polling and repeated API calls from flooding a
    chat while still retrying after a transient Telegram failure.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._states: dict[str, str] = {}
        self._events: set[str] = set()

    def notify_state(self, channel: str, state: str, message: str) -> bool:
        if self._states.get(channel) == state:
            return False
        sent = send_telegram_message(self.path, message)
        if sent:
            self._states[channel] = state
        return sent

    def notify_event(self, event_key: str, message: str) -> bool:
        if event_key in self._events:
            return False
        sent = send_telegram_message(self.path, message)
        if sent:
            self._events.add(event_key)
        return sent


def _secrets(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() and value.strip():
            values[key.strip()] = value.strip()
    return values


def send_telegram_message(path: Path | None, message: str, *, timeout: float = 5.0) -> bool:
    """Send an optional redacted operational notification; never raises to the worker."""
    values = _secrets(path)
    token = values.get("TELEGRAM_BOT_TOKEN")
    chat_id = values.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    request = Request(
        endpoint,
        data=urlencode({"chat_id": chat_id, "text": message[:3900]}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed Telegram endpoint
            return 200 <= response.status < 300
    except Exception:  # noqa: BLE001 - notification failure cannot stop trading monitor
        return False
