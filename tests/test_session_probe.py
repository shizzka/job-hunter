from contextlib import contextmanager
from types import SimpleNamespace

from hh import session_probe as sp

CATALOG = ('<html><body><div data-qa="resume"></div>'
           '<a href="/resume/abc123">Постоянная работа Электромонтажник 200 000 ₽</a>'
           '<a href="/resume/def456">Тестировщик</a></body></html>')


def factory(status, text, url="https://hh.ru/applicant/resumes"):
    @contextmanager
    def make(cookies_file):
        yield SimpleNamespace(get=lambda u: SimpleNamespace(status_code=status, text=text, url=url))
    return make


def test_live_catalog():
    result = sp.probe("x", client_factory=factory(200, CATALOG))
    assert result["status"] == sp.OK
    assert [r["title"] for r in result["resumes"]] == ["Электромонтажник", "Тестировщик"]


def test_captcha_and_login_and_error():
    assert sp.probe("x", client_factory=factory(403, "<html>captcha</html>"))["status"] == sp.CHALLENGE
    assert sp.probe("x", client_factory=factory(200, "<html></html>", "https://hh.ru/account/login?x=1"))["status"] == sp.LOGIN

    @contextmanager
    def broken(cookies_file):
        raise OSError("offline")
        yield
    assert sp.probe("x", client_factory=broken)["status"] == sp.ERROR
