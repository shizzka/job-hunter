"""Notifier transport reaches only a synthetic loopback sink."""
import asyncio
from unittest.mock import AsyncMock
import pytest
import notifier
import aiohttp
from aiohttp import web
import hh_chat_responder as cr
from tests.test_browser_boundary_independent import browser_case, evidence
from tests.test_browser_boundary_workflows import install_chat, chat_setup

def _native_notifier_actual_transport_timeout(tmp_path,monkeypatch,proxy,response="timeout"):
    async def run(context,page):
        await install_chat(context,page)
        paths,client,repo=chat_setup(monkeypatch,page,tmp_path)
        monkeypatch.setattr(cr,'generate_answer',AsyncMock(return_value='Short answer'))
        original_fill=cr.fill_and_preview
        async def no_screenshot(*args,**kwargs):
            result=await original_fill(*args,**kwargs);result['screenshot_path']='';return result
        monkeypatch.setattr(cr,'fill_and_preview',no_screenshot)
        received=[]
        async def sink(request):
            received.append(await request.json())
            if response=='timeout':
                await asyncio.sleep(.15)
                return web.json_response({'ok':True})
            return web.json_response({'ok':False},status=503 if response=='false' else 200)
        app=web.Application();app.router.add_post('/telegram',sink)
        runner=web.AppRunner(app);await runner.setup()
        server=web.TCPSite(runner,'127.0.0.1',0);await server.start()
        port=server._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=.04 if response=='timeout' else 2)) as session:
                original_request=session._request
                async def local_request(method,url,*args,**kwargs):
                    return await original_request(method,f'http://127.0.0.1:{port}/telegram',*args,**kwargs)
                monkeypatch.setattr(session,'_request',local_request)
                monkeypatch.setattr(notifier,'_get_session',AsyncMock(return_value=session))
                monkeypatch.setattr(notifier,'capture_delivery_target',lambda:('synthetic-token',(1,),'synthetic-proxy' if proxy else ''))
                first=await cr.process_one(client,'a',message_id='m1',dry_run=True,notify=True,runtime_paths=paths)
                first_count=len(received);first_status=next(v['status'] for v in repo.load()['a']['attempts'].values())
                alternative=await cr.process_one(client,'a',message_id='m1',dry_run=True,notify=True,alternative=True,runtime_paths=paths)
                evidence('n2_real_transport_timeout_'+str(proxy),first_ok=first['ok'],notification_uncertain=first.get('notification_uncertain',False),first_status=first_status,first_requests=first_count,alternative_blocked=alternative.get('blocked',False),total_requests=len(received))
                assert first_count==1, 'Never proxy/direct replay a dispatched HTTP request'
                assert first_status=='uncertain'
                assert first.get('notification_uncertain'), 'Actual transport timeout must propagate delivery uncertainty'
                assert alternative.get('blocked') and len(received)==first_count
        finally:await runner.cleanup()
    asyncio.run(browser_case(run))


@pytest.mark.parametrize('proxy',[False,True])
@pytest.mark.parametrize('response',['timeout','false','api_false'])
def test_n2_native_notifier_actual_transport_timeout(tmp_path, proxy, response):
    # Ordinary pytest's network guard is unchanged. This isolated child routes
    # every notifier request to one bound loopback sink; browser traffic is synthetic.
    import os
    import subprocess
    import sys
    from pathlib import Path
    env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'LANG':'C.UTF-8',
         'HOME':str(tmp_path),'PLAYWRIGHT_BROWSERS_PATH':os.environ['PLAYWRIGHT_BROWSERS_PATH']}
    code="from pathlib import Path; from pytest import MonkeyPatch; from tests.test_browser_notifier_boundary import _native_notifier_actual_transport_timeout; import sys; _native_notifier_actual_transport_timeout(Path(sys.argv[1]), MonkeyPatch(), sys.argv[2]=='True',sys.argv[3])"
    result=subprocess.run([sys.executable,'-c',code,str(tmp_path),str(proxy),response],env=env,
                          cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=60)
    assert result.returncode==0, result.stdout+result.stderr
