from telegram_app import onboarding


def actions(rows):
    return [action for row in rows for _, action in row]


def test_fresh_user_sees_login_first():
    text, rows, complete = onboarding.build(hh_ok=False, resumes=[], directions=[], active=None, mode="", has_search=False)
    assert not complete and actions(rows)[0] == "auth"
    assert "new" not in actions(rows) and "search" not in actions(rows)
    assert "1. Войти в hh.ru" in text


def test_resumes_without_direction_offer_creation():
    _, rows, complete = onboarding.build(hh_ok=True, resumes=[{"title": "Тестировщик"}], directions=[], active=None,
                                         mode="confirm", has_search=False)
    assert "new" in actions(rows) and "search" not in actions(rows) and not complete


def test_ready_profile_offers_search_and_completes_after_first_run():
    active = {"title": "Электромонтажник"}
    text, rows, complete = onboarding.build(hh_ok=True, resumes=[{"title": "A"}], directions=[active], active=active,
                                            mode="confirm", has_search=False)
    assert "search" in actions(rows) and not complete and "Направления: Электромонтажник" in text
    _, _, complete = onboarding.build(hh_ok=True, resumes=[{"title": "A"}], directions=[active], active=active,
                                      mode="confirm", has_search=True)
    assert complete


def test_every_action_maps_to_an_existing_command():
    import telegram_bot_ui as ui
    known = set(ui.ADMIN_BUTTON_MAP.values()) | set(ui.USER_BUTTON_MAP.values())
    for command in onboarding.ACTIONS.values():
        assert command in known or command in {"/hh_resumes", "/directions"}, command
