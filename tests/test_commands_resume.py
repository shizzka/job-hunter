import asyncio

from commands import resume


class FakeClient:
    instances = []
    logged_in = True
    detail = {}

    def __init__(self):
        self.started = False
        self.stopped = False
        self.status_kwargs = None
        self.boost_kwargs = None
        self.__class__.instances.append(self)

    async def start(self):
        self.started = True

    async def is_logged_in(self):
        return self.logged_in

    async def get_resume_boost_status(self, **kwargs):
        self.status_kwargs = kwargs
        return dict(self.detail)

    async def boost_resume(self, **kwargs):
        self.boost_kwargs = kwargs
        return dict(self.detail)

    async def stop(self):
        self.stopped = True


def _install_fake_client(monkeypatch, *, logged_in=True, detail=None):
    FakeClient.instances = []
    FakeClient.logged_in = logged_in
    FakeClient.detail = detail or {}
    monkeypatch.setattr(resume, "HHClient", FakeClient)


def test_resume_boost_status_preserves_output_and_stops_client(monkeypatch, capsys):
    detail = {
        "ok": True,
        "title": "QA Engineer",
        "resume_id": "resume-1",
        "can_boost": True,
        "reason": "available",
        "button_text": "Поднять",
        "resumes_found": 2,
    }
    _install_fake_client(monkeypatch, detail=detail)
    monkeypatch.setattr(resume.config, "HH_PRIMARY_RESUME_ID", "resume-1")
    monkeypatch.setattr(resume.config, "HH_PRIMARY_RESUME_TITLE", "QA Engineer")

    asyncio.run(resume.do_hh_resume_boost_status())

    client = FakeClient.instances[0]
    assert client.status_kwargs == {
        "resume_id": "resume-1",
        "resume_title": "QA Engineer",
    }
    assert client.stopped is True
    assert capsys.readouterr().out == (
        "📌 HH resume boost:\n"
        "  OK: True\n"
        "  Резюме: QA Engineer (resume-1)\n"
        "  Можно поднять: да\n"
        "  Причина: available\n"
        "  Кнопка: Поднять\n"
        "  Найдено резюме: 2\n"
    )


def test_resume_boost_status_preserves_login_guard(monkeypatch, capsys):
    _install_fake_client(monkeypatch, logged_in=False)

    asyncio.run(resume.do_hh_resume_boost_status())

    assert capsys.readouterr().out == "❌ Не залогинен! Сначала: ./run.sh login\n"
    assert FakeClient.instances[0].stopped is True


def test_resume_boost_checks_feature_flag_before_creating_client(monkeypatch, capsys):
    monkeypatch.setattr(resume.config, "HH_RESUME_BOOST_ENABLED", False)
    monkeypatch.setattr(resume, "HHClient", lambda: (_ for _ in ()).throw(AssertionError("client created")))

    asyncio.run(resume.do_hh_resume_boost("ПОДНЯТЬ"))

    assert capsys.readouterr().out == (
        "❌ Поднятие отключено: выставь HH_RESUME_BOOST_ENABLED=1 для ручного запуска.\n"
    )


def test_resume_boost_preserves_confirmation_and_client_arguments(monkeypatch, capsys):
    detail = {
        "ok": True,
        "title": "QA Engineer",
        "resume_id": "resume-1",
        "can_boost": False,
        "reason": "boosted",
        "resumes_found": 1,
    }
    _install_fake_client(monkeypatch, detail=detail)
    monkeypatch.setattr(resume.config, "HH_RESUME_BOOST_ENABLED", True)
    monkeypatch.setattr(resume.config, "HH_RESUME_BOOST_CONFIRM_TEXT", "ПОДНЯТЬ")
    monkeypatch.setattr(resume.config, "HH_PRIMARY_RESUME_ID", "resume-1")
    monkeypatch.setattr(resume.config, "HH_PRIMARY_RESUME_TITLE", "QA Engineer")

    asyncio.run(resume.do_hh_resume_boost("ПОДНЯТЬ"))

    client = FakeClient.instances[0]
    assert client.boost_kwargs == {
        "resume_id": "resume-1",
        "resume_title": "QA Engineer",
        "confirm": "ПОДНЯТЬ",
    }
    assert client.stopped is True
    assert "  Можно поднять: нет\n" in capsys.readouterr().out
