"""Both decision-card entry points send readable plain-text explanations."""
import asyncio

import pytest

import manual_apply_queue as queue
from tests.test_f27_review import setup, tap
from tests.test_telegram_callbacks import _callback, _configured_bot


@pytest.mark.parametrize('entry', ['current', 'legacy'])
def test_why_explains_scores_without_markup_or_internal_fields(setup, monkeypatch, entry):
    fake, principal, items = setup
    item = items[0]
    queue._store('qa').update(lambda data: data['items'][item['token']].update(
        vacancy={**item['vacancy'], 'title': 'QA & API', 'company': 'A & B'},
        evaluation={'score': 85, 'response_probability_score': 77,
                    'cluster': 'enterprise_banking_qa', 'resume_variant': 'qa_internal',
                    'cover_style': 'enterprise_process', 'guard_flags': ['below_auto_apply_threshold'],
                    'reason': 'API & SQL подходят по опыту.'}))
    before = queue._read_queue('qa')
    if entry == 'current':
        for _ in range(2):
            tap(fake, principal, 'w', item['token'])
        text = fake.events[-1][1]
    else:
        bot, calls = _configured_bot(monkeypatch, principal=principal)
        monkeypatch.setattr(bot, '_profile', fake._profile)
        for _ in range(2):
            asyncio.run(bot._handle_callback_query(_callback(
                f'manual_why:qa:{item["token"]}', user_id=42, chat_type='private')))
        text = calls['messages'][-1][1]
    assert 'Подходит на 85/100' in text and '77/100' in text
    assert 'QA & API' in text and 'A & B' in text and 'API & SQL' in text
    for forbidden in ['<b>', '</b>', '&amp;', 'Score:', 'Кластер:', 'Сопровод:',
                      'enterprise_banking_qa', 'enterprise_process', 'qa_internal',
                      'below_auto_apply_threshold']:
        assert forbidden not in text
    assert queue._read_queue('qa') == before


@pytest.mark.parametrize('salary', ['', ' не указана', 'не указано', 'зарплата не указана', None])
def test_missing_salary_has_a_complete_label(salary):
    text = queue.build_review_card_text({'vacancy': {'title': 'QA', 'salary': salary}})
    assert '💰 Зарплата не указана' in text


def test_review_owner_can_open_card_from_another_search(setup):
    from tests.direction_view_fakes import two_directions
    bot, principal, items = setup
    two_directions(bot.home)
    queue._store('qa').update(lambda data: data['items'][items[0]['token']].update(direction='electric'))
    asyncio.run(bot._open_review_card(42, 'qa', items[0]['token']))
    assert bot.events[-1][1].startswith('📋 Ждут решения · 1 из 1')
    assert 'Job 0' in bot.events[-1][1]
    assert 'Вакансии на проверку' not in bot.events[-1][1]
    assert bot.events[-1][2]['inline_keyboard'][0][0]['text'] == '✅ Откликнуться'
