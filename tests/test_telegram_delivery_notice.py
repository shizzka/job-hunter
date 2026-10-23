"""Delivery uncertainty must request reconciliation, never another application."""
import asyncio
from unittest.mock import AsyncMock

import notifier


def test_uncertain_notice_requests_verification_instead_of_manual_apply(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(notifier, "send_message", send)
    asyncio.run(notifier.notify_needs_manual({"title": "Synthetic", "source": "hh"}, 80, "review",
        note="Отклик мог быть отправлен; автоматического повтора нет.", uncertain=True))
    text = send.call_args.args[0]
    assert "❔ Исход отклика не подтверждён" in text
    assert "нужна ручная сверка" in text and "Открыть и проверить" in text
    assert "нужен ручной отклик" not in text and "Открыть и откликнуться" not in text
    assert "❌" not in text


def test_ordinary_manual_notice_keeps_original_wording(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(notifier, "send_message", send)
    asyncio.run(notifier.notify_needs_manual({"title": "Synthetic"}, 80, "review"))
    text = send.call_args.args[0]
    assert "Вакансия ждёт вашего решения" in text and "Открыть и откликнуться" in text  # F23 wording
