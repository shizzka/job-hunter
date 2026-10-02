import html
from html.parser import HTMLParser

import pytest

import notifier
from telegram_app.formatting import limit_telegram_text


class _CheckedHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)

    def handle_endtag(self, tag):
        assert self.tags.pop() == tag

    def handle_data(self, data):
        self.text.append(data)


def _visible(text):
    parser = _CheckedHTML()
    parser.feed(text)
    parser.close()
    assert parser.tags == []
    return "".join(parser.text)


@pytest.mark.parametrize(
    "text,limit,expected",
    [
        ("<b>abcdef</b>", 3, "<b>abc</b>"),
        ("<b><i>abcdef</i></b>", 3, "<b><i>abc</i></b>"),
        ("<b>abc</b><i>def</i>", 3, "<b>abc</b>"),
        ("<code>ab&amp;cd</code>", 3, "<code>ab&amp;</code>"),
        ("<b>ab&#38;cd</b>", 3, "<b>ab&#38;</b>"),
        ("<b>ab&#x26;cd</b>", 3, "<b>ab&#x26;</b>"),
        ("<b>&lt;&gt;&amp;&quot;tail</b>", 4, "<b>&lt;&gt;&amp;&quot;</b>"),
        ("<b>Ж😀tail</b>", 2, "<b>Ж</b>"),
        ("<b>Ж😀tail</b>", 3, "<b>Ж😀</b>"),
        ("<b>ab&#x1F600;cd</b>", 3, "<b>ab</b>"),
        ("<b>ab&#x1F600;cd</b>", 4, "<b>ab&#x1F600;</b>"),
        ("<b>abcdef</b>", 0, ""),
        ("<pre><code class=\"language-python\">abcdef</code></pre>", 3,
         "<pre><code class=\"language-python\">abc</code></pre>"),
    ],
)
def test_html_prefix_closes_tags_and_preserves_entities(text, limit, expected):
    result = limit_telegram_text(text, limit, "HTML")

    assert result == expected
    assert len(_visible(result).encode("utf-16-le")) // 2 <= limit


def test_link_attributes_do_not_count_towards_visible_limit():
    text = '<a href="https://example.test/' + "x" * 5000 + '?a=1&amp;b=2">ab&amp;cd</a>'
    result = limit_telegram_text(text, 3, "html")

    assert result.endswith('">ab&amp;</a>')
    assert 'a=1&amp;b=2' in result
    assert _visible(result) == "ab&"


def test_long_encoded_text_within_visible_limit_is_not_lost():
    text = "<b>" + "&amp;" * 100 + "</b>"

    assert limit_telegram_text(text, 100, "HTML") == text


def test_short_html_is_unchanged():
    text = '<a href="https://example.test">&quot;Ж&quot;</a>'

    assert limit_telegram_text(text, 4096, "HTML") == text


@pytest.mark.parametrize("mode", ["", "MarkdownV2", None])
def test_non_html_text_is_not_decoded(mode):
    assert limit_telegram_text("<b>&amp;</b>", 5, mode) == "<b>&a"


def test_plain_text_emoji_is_not_split():
    assert limit_telegram_text("Ж😀tail", 2) == "Ж"
    assert limit_telegram_text("Ж😀tail", 3) == "Ж😀"


@pytest.mark.parametrize("limit", [1024, 4096])
def test_large_nested_notification_fits_visible_limit(limit):
    value = "Работа & практика 😀 " * 500
    text = '<b>Вакансия</b>\n<code>' + html.escape(value) + '</code>'

    result = limit_telegram_text(text, limit, "HTML")

    assert len(_visible(result).encode("utf-16-le")) // 2 <= limit
    assert result.endswith("</code>")


def test_negative_limit_is_rejected():
    with pytest.raises(ValueError):
        limit_telegram_text("abc", -1, "HTML")


def test_text_payload_limits_html_without_breaking_tags():
    text = "<b><code>" + "&amp;" * 5000 + "</code></b>"
    markup = {"inline_keyboard": []}
    factory = notifier._build_text_payload(text, "HTML", markup)

    first = factory(111)
    second = factory(222)

    assert first["text"] == "<b><code>" + "&amp;" * 4096 + "</code></b>"
    assert second["text"] == first["text"]
    assert first["reply_markup"] == markup
    assert first["chat_id"] == 111
    assert second["chat_id"] == 222


def test_photo_form_limits_html_caption_and_keeps_keyboard(monkeypatch, tmp_path):
    class FakeForm:
        def __init__(self):
            self.fields = {}

        def add_field(self, name, value, **kwargs):
            self.fields[name] = value

    monkeypatch.setattr(notifier.aiohttp, "FormData", FakeForm)
    photo = tmp_path / "photo.png"
    photo.write_bytes(b"png")
    caption = "<b>" + "&#x1F600;" * 700 + "</b>"
    markup = {"inline_keyboard": []}
    factory = notifier._build_photo_form(str(photo), caption, "HTML", markup)

    for chat_id in (111, 222):
        form = factory(chat_id)
        try:
            assert form.fields["caption"] == "<b>" + "&#x1F600;" * 512 + "</b>"
            assert form.fields["parse_mode"] == "HTML"
            assert form.fields["chat_id"] == str(chat_id)
            assert form.fields["reply_markup"] == '{"inline_keyboard": []}'
            assert not form.fields["photo"].closed
        finally:
            form.fields["photo"].close()
