import asyncio

import client_hh_auth as auth

PHONE_PAGE = ('<html><body><div>Поиск работы Телефон +7 911 Дальше</div>'
              '<template id="HH-Lux-InitialState">{"t":"Введите код подтверждения, мы отправили код"}</template>'
              '<script>window.t="введите код"</script></body></html>')


class BrokenTextPage:
    """inner_text and evaluate fail, as under load; only raw HTML is available."""
    url = "https://hh.ru/account/login"

    def locator(self, selector):
        class L:
            async def inner_text(self, timeout=0):
                raise TimeoutError("slow")
        return L()

    async def evaluate(self, script):
        raise RuntimeError("no js")

    async def content(self):
        return PHONE_PAGE


def test_raw_fallback_ignores_embedded_state_phrases():
    text = asyncio.run(auth._hh_auth_page_text(BrokenTextPage()))
    assert "дальше" in text and "введите код" not in text
    assert not auth._looks_like_hh_auth_code_prompt(text, BrokenTextPage.url)


def test_email_login_switches_to_mail_and_types(monkeypatch):
    calls = []

    class Input:
        def __init__(self, name): self.name = name
        async def click(self, timeout=0, force=False): calls.append(("click", self.name))
        async def fill(self, value, timeout=0): calls.append(("fill", self.name, value))
        async def press_sequentially(self, value, delay=0): calls.append(("type", self.name, value))

    class Page:
        url = "https://hh.ru/account/login"
        switched = False
        async def wait_for_timeout(self, ms): pass

    page = Page()

    async def first_visible(pg, selectors):
        if selectors is auth.HH_AUTH_EMAIL_INPUT_SELECTORS:
            return Input("email") if pg.switched else None
        if selectors is auth.HH_AUTH_EMAIL_MODE_SELECTORS:
            class Tab(Input):
                async def click(self, timeout=0, force=False):
                    pg.switched = True; calls.append(("click", "tab"))
            return Tab("tab")
        return Input("phone")

    monkeypatch.setattr(auth, "_first_visible_locator", first_visible)
    receipts = []
    assert asyncio.run(auth._fill_hh_auth_login(page, " m@example.com ", receipts=receipts))
    assert ("click", "tab") in calls and ("type", "email", "m@example.com") in calls
    assert not any(c[1] == "phone" for c in calls if len(c) > 1)
