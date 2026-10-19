import hh_chat_responder as cr


def test_english_message_asks_for_english_answer():
    text = ("You can continue your search on the platform. It has jobs from other companies and free "
            "job-search tools.")
    assert "на английском" in cr._language_instruction(text)


def test_russian_or_mixed_message_keeps_russian():
    assert cr._language_instruction("Какой у вас опыт с Postman и REST API?") == ""
    assert cr._language_instruction("Опыт с QA, API, SQL, Jira?") == ""
    assert cr._language_instruction("") == ""
