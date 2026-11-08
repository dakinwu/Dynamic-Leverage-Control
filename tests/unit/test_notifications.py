from pathlib import Path

from dynamic_leverage.services import notifications
from dynamic_leverage.services.notifications import TelegramNotifier, send_telegram_message


def test_telegram_notification_is_optional_without_credentials(tmp_path: Path) -> None:
    secret_file = tmp_path / "runtime.env"
    secret_file.write_text("SJ_API_KEY=key\n", encoding="utf-8")
    assert send_telegram_message(secret_file, "not sent") is False


def test_telegram_notifier_deduplicates_unchanged_state(monkeypatch) -> None:
    calls: list[str] = []

    def fake_send(_path, message: str) -> bool:
        calls.append(message)
        return True

    monkeypatch.setattr(notifications, "send_telegram_message", fake_send)
    notifier = TelegramNotifier(None)

    assert notifier.notify_state("risk_gate", "BLOCKED:DATA_UNSAFE", "blocked") is True
    assert notifier.notify_state("risk_gate", "BLOCKED:DATA_UNSAFE", "blocked again") is False
    assert notifier.notify_state("risk_gate", "APPROVED", "recovered") is True
    assert calls == ["blocked", "recovered"]
