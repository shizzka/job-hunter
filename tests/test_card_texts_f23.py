"""F23: the candidate's card says the work format and uses plain words instead of «N/100» (synthetic data)."""
import asyncio
from unittest.mock import AsyncMock

import pytest

import config
import notifier


def card(monkeypatch, vacancy, score, reason="Опыт совпадает", **kwargs):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "send_message", sent)
    asyncio.run(notifier.notify_needs_manual(vacancy, score, reason, **kwargs))
    return sent.await_args.args[0]


@pytest.mark.parametrize("formats,fly,line", [
    (["remote"], False, "🏠 Удалённо"),
    (["hybrid", "remote"], False, "📍 Формат работы: удалённо, гибрид"),
    (["on_site"], False, "📍 Формат работы: на месте работодателя"),
    ([], False, "❔ Формат работы не указан"),
    (["unknown"], False, "❔ Формат работы не указан"),
    (["remote"], True, "🧳 Вахта"),
])
def test_card_shows_work_format(monkeypatch, formats, fly, line):
    vacancy = {"title": "Специалист по данным", "source": "hh",
               "_hh_work_conditions": {"formats": formats, "fly_in_fly_out": fly}}
    assert line in card(monkeypatch, vacancy, 70)


def test_other_sources_have_no_format_line(monkeypatch):
    text = card(monkeypatch, {"title": "QA", "source": "habr"}, 70)
    assert "Формат работы" not in text and "Удалённо" not in text


@pytest.mark.parametrize("delta,label", [(0, "✅ Подходит под ваше резюме"), (-10, "👍 Скорее подходит"),
                                         (-30, "🤔 Подходит частично — решать вам")])
def test_score_has_words_and_the_number(monkeypatch, delta, label):
    score = config.HH_MATCHER_AUTO_APPLY_MIN_SCORE + delta
    text = card(monkeypatch, {"title": "QA", "source": "hh"}, score)
    assert label in text
    assert f"{label} · {score}/100" in text and "Совпадение" not in text and "score" not in text
    assert "Вакансия ждёт вашего решения" in text and "нужен ручной отклик" not in text


def test_sent_application_card_uses_the_same_words(monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "send_message", sent)
    monkeypatch.setattr(notifier, "_should_notify_application", lambda note: True)
    vacancy = {"title": "QA", "source": "hh", "_hh_work_conditions": {"formats": ["remote"], "fly_in_fly_out": False}}
    asyncio.run(notifier.notify_application(vacancy, config.HH_MATCHER_AUTO_APPLY_MIN_SCORE, ""))
    text = sent.await_args.args[0]
    assert "🏠 Удалённо" in text and "✅ Подходит под ваше резюме · " in text
