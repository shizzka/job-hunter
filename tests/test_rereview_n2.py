"""Local preview validation is distinct from an uncertain notifier transport."""
import asyncio
import html
from unittest.mock import AsyncMock

import pytest
import notifier
import hh_chat_responder as cr
from tests.test_chat_workflow_transactions import workflow, attempt_status


@pytest.mark.parametrize('oversized', ['x' * 5000, '<' * 1100], ids=['raw_limit','escaped_limit'])
@pytest.mark.parametrize('screenshot',[False,True])
def test_local_oversized_preview_allows_short_alternative(workflow,monkeypatch,oversized,screenshot):
    answers=iter([oversized,'Short approved alternative'])
    generations=[]; deliveries=[]
    async def answer(*args,**kwargs):
        generations.append(kwargs.get('alternative',False))
        return next(answers)
    async def photo(path,**kwargs):deliveries.append(('photo',kwargs.get('caption')));return True
    async def message(caption,**kwargs):deliveries.append(('message',caption));return True
    monkeypatch.setattr(cr,'generate_answer',answer)
    monkeypatch.setattr(cr,'fill_and_preview',AsyncMock(return_value={'filled':True,'screenshot_path':'synthetic.png' if screenshot else ''}))
    monkeypatch.setattr(notifier,'send_photo',photo)
    monkeypatch.setattr(notifier,'send_message_with_markup',message)
    async def run():
        rejected=await workflow.run(dry_run=True,notify=True)
        assert deliveries==[]  # Local limit check precedes every transport call.
        assert not rejected['ok'] and rejected['notification_rejected']
        assert attempt_status(workflow)=='failed'
        state=workflow.repo.load()['chat']
        assert not state.get('draft') and not state.get('last_previewed_msg_id')
        alternative=await cr.process_one(workflow.client,'chat',message_id='m1',dry_run=True,notify=True,
                                         alternative=True,runtime_paths=workflow.paths)
        assert alternative['ok'] and not alternative.get('blocked')
        assert alternative['answer']=='Short approved alternative'
        assert attempt_status(workflow)=='completed'
        assert workflow.repo.load()['chat']['draft']['answer']==alternative['answer']
        assert len(deliveries)==1
        assert html.escape(alternative['answer']) in deliveries[0][1]
        assert generations==[False,True]
    asyncio.run(run())


def test_notifier_exception_after_transport_remains_uncertain(workflow,monkeypatch):
    deliveries=[]
    async def transport(*args,**kwargs):
        deliveries.append('transport')
        raise ValueError('Synthetic transport may have delivered')
    monkeypatch.setattr(notifier,'send_message_with_markup',transport)
    async def run():
        result=await workflow.run(dry_run=True,notify=True)
        assert result['notification_uncertain']
        assert not result.get('notification_rejected')
        assert deliveries==['transport'] and attempt_status(workflow)=='uncertain'
        alternative=await cr.process_one(workflow.client,'chat',message_id='m1',dry_run=True,notify=True,
                                         alternative=True,runtime_paths=workflow.paths)
        assert alternative['blocked'] and deliveries==['transport']
    asyncio.run(run())


@pytest.mark.parametrize('partial',[False,True])
def test_false_photo_or_partial_preview_is_uncertain_and_not_replayed(workflow,monkeypatch,partial):
    deliveries=[]
    monkeypatch.setattr(cr,'generate_answer',AsyncMock(return_value='x'*1300 if partial else 'Short answer'))
    monkeypatch.setattr(cr,'fill_and_preview',AsyncMock(return_value={'filled':True,'screenshot_path':'synthetic.png'}))
    async def photo(*args,**kwargs):
        deliveries.append('photo')
        return partial
    async def message(*args,**kwargs):
        deliveries.append('message')
        return False
    monkeypatch.setattr(notifier,'send_photo',photo)
    monkeypatch.setattr(notifier,'send_message_with_markup',message)
    async def run():
        result=await workflow.run(dry_run=True,notify=True)
        expected=['photo','message'] if partial else ['photo']
        assert result['notification_uncertain'] and attempt_status(workflow)=='uncertain'
        assert deliveries==expected
        alternative=await cr.process_one(workflow.client,'chat',message_id='m1',dry_run=True,
            notify=True,alternative=True,runtime_paths=workflow.paths)
        assert alternative['blocked'] and deliveries==expected
    asyncio.run(run())
