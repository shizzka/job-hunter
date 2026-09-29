import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import matcher
from hh import apply as hh_apply


def test_appliance_jobs_do_not_pass_as_software_qa():
    for title, details in [
        ('Тестировщик техники', 'Испытания бытовой техники, проверка качества'),
        ('Тестировщик бытовой техники', 'Диагностика и ремонт'),
        ('Ассистент тестировщика', 'Тестирование и обслуживание промышленного оборудования'),
    ]:
        result = matcher._add_strategy_fields({'score': 85, 'should_apply': True}, {'title': title}, details)
        assert result['cluster'] == 'reject_non_qa'
        assert result['should_apply'] is False


def test_software_testing_with_devices_is_not_blanket_blocked():
    assert matcher.classify_vacancy_cluster({'title': 'Тестировщик устройств и программного обеспечения'}, 'Тестирование API, Postman и веб-приложений') != 'reject_non_qa'


def test_send_click_is_not_delivery_confirmation():
    field = AsyncMock()
    page = AsyncMock()
    page.frames = []
    page.evaluate.return_value = ''
    async def query(selector):
        return None if selector.startswith("form[action") else field
    page.query_selector.side_effect = query
    session = SimpleNamespace(_page=page, _expand_cover_letter_input=AsyncMock())
    logger = Mock()
    confirmed = asyncio.run(hh_apply.fill_cover_letter_post_apply(session, 'My cover letter', logger=logger))
    assert confirmed is False
    assert any('NOT confirmed' in str(call) for call in logger.warning.call_args_list)
    assert not any('delivery confirmed' in str(call) for call in logger.info.call_args_list)


def test_visible_letter_confirms_delivery_without_resending():
    page = AsyncMock()
    page.frames = []
    page.evaluate.return_value = 'My cover letter'
    session = SimpleNamespace(_page=page, _expand_cover_letter_input=AsyncMock())
    assert asyncio.run(hh_apply.fill_cover_letter_post_apply(session, 'My cover letter', logger=Mock())) is True
    page.query_selector.assert_not_awaited()


def exact_form_session(value='My cover letter', visible='My cover letter'):
    field, button, form, page = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    field.input_value.return_value = value
    async def query(selector):
        return field if selector.startswith('textarea') else button
    form.query_selector.side_effect = query
    page.query_selector.return_value = form
    page.evaluate.return_value = visible
    return SimpleNamespace(_page=page, _save_debug_snapshot=AsyncMock()), field, button


def test_exact_form_uses_own_submit_button():
    session, field, button = exact_form_session()
    assert asyncio.run(hh_apply._send_response_letter_form(session, 'My cover letter', logger=Mock())) is True
    field.fill.assert_awaited_once_with('My cover letter')
    button.click.assert_awaited_once_with(timeout=10000)
    field.press.assert_not_awaited()


def test_exact_form_will_not_send_empty_or_lost_draft():
    session, field, button = exact_form_session(value='')
    assert asyncio.run(hh_apply._send_response_letter_form(session, 'My cover letter', logger=Mock())) is False
    button.click.assert_not_awaited()


def test_exact_form_failed_confirmation_saves_snapshot_without_generic_retry():
    session, field, button = exact_form_session(visible='')
    assert asyncio.run(hh_apply.fill_cover_letter_post_apply(session, 'My cover letter', logger=Mock())) is False
    session._save_debug_snapshot.assert_awaited_once_with('debug_cover_letter_unconfirmed')
    button.click.assert_awaited_once()
    field.press.assert_not_awaited()


def test_exact_form_click_failure_does_not_press_enter():
    session, field, button = exact_form_session()
    button.click.side_effect = TimeoutError()
    assert asyncio.run(hh_apply._send_response_letter_form(session, 'My cover letter', logger=Mock())) is False
    field.press.assert_not_awaited()
