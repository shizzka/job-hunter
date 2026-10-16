import pytest

from chat_triage import decide_chat, external_invitation


def incoming(text="Когда сможете приступить?", **kwargs):
    return dict(id="1", text=text, is_me=False, **kwargs)


@pytest.mark.parametrize("text,decision", [
    ("Когда сможете приступить?", "hr_question"),
    ("Пришлите портфолио", "hr_question"),
    ("Предлагаем обсудить детали завтра", "hr_question"),
    ("Спасибо!", "no_question"),
    ("Работодатель просмотрел ваше резюме", "system_notice"),
    ("Пройдите тест https://example.org/assessment/1", "external_invite"),
    ("Напишите нашему боту t.me/synthetic_bot", "external_invite"),
])
def test_chat_action_is_separate_from_author(text, decision):
    assert decide_chat([incoming(text)], {}, dry_run=False, max_replies=5)[0] == decision


def test_chat_hh_ai_lane_is_preserved_and_ours_last_does_not_resurrect_question():
    question = incoming(is_ai=True)
    assert decide_chat([question], {}, dry_run=False, max_replies=5)[0] == "ai_hh_autoreply"
    ours = dict(id="2", text="С понедельника", is_me=True)
    assert decide_chat([question, ours], {}, dry_run=False, max_replies=5)[0] == "ours_last"
    new = incoming("А в офис готовы?", is_ai=False)
    new["id"] = "3"
    decision, reason, target = decide_chat([question, ours, new], {}, dry_run=False, max_replies=5)
    assert decision == "hr_question" and target["id"] == "3"


@pytest.mark.parametrize("url", ["t.me/synthetic_bot", "https://t.me/synthetic_bot?start=x",
    "telegram.me/synthetic_bot", "tg://resolve?domain=synthetic_bot"])
def test_chat_telegram_invites_in_text_and_link_only_cards(url):
    assert external_invitation(incoming(url))["urls"]
    assert external_invitation(incoming("", links=[{"href": url}]))["urls"]


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://t.me.evil.example/hello",
    "https://hh.ru/vacancy/42", "https://forms.gle/synthetic", "https://example.org/about"])
def test_chat_not_every_link_is_an_external_invite(url):
    assert external_invitation(incoming(url, links=[{"href": url}])) == {}


def test_chat_missing_id_and_limits_fail_closed():
    assert decide_chat([dict(text="Вопрос?")], {}, dry_run=False, max_replies=5)[0] == "unknown"
    assert decide_chat([incoming()], {"replies_count": 5}, dry_run=False, max_replies=5)[0] == "limit"
    assert decide_chat([incoming()], {"last_replied_msg_id": "1"}, dry_run=False, max_replies=5)[0] == "already_handled"
