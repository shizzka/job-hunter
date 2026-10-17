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
