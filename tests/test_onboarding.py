import asyncio
import json
from itertools import product
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

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


@pytest.mark.parametrize("done", list(product((False, True), repeat=5)))
def test_each_step_mark_uses_its_own_state_and_summary_requires_all(done):
    hh_ok, has_resumes, has_direction, has_mode, has_search = done
    active = {"title": "Направление А"} if has_direction else None
    text, rows, complete = onboarding.build(
        hh_ok=hh_ok, resumes=[{"title": "Резюме А"}] if has_resumes else [],
        directions=[active] if active else [], active=active,
        mode="confirm" if has_mode else "", has_search=has_search,
    )
    for step, ok in enumerate(done, 1):
        assert f"{'✅' if ok else '⬜️'} {step}." in text
    assert complete == all(done)
    assert ("Всё готово!" in text) == all(done)
    assert ("Дальше бот присылает вакансии сам." in text) == all(done)
    assert ("search" in actions(rows)) == all(done[:4])
    assert "3. Направления" in text


@pytest.mark.parametrize("mode", ["confirm", "preview", "auto", "", "unknown"])
def test_mode_is_done_only_for_a_supported_effective_mode(mode):
    active = {"title": "Направление А"}
    text, rows, complete = onboarding.build(
        hh_ok=True, resumes=[{"title": "Резюме А"}], directions=[active], active=active,
        mode=mode, has_search=True,
    )
    valid = mode in {"confirm", "preview", "auto"}
    assert f"{'✅' if valid else '⬜️'} 4." in text
    assert complete == valid
    assert ("search" in actions(rows)) == valid


@pytest.mark.parametrize("total", range(9))
def test_resume_total_and_hidden_remainder_use_real_entries(total):
    resumes = [{"id": f"synthetic-{i}", "title": f"Резюме {i}, профессия {i}"} for i in range(total)]
    text, _, _ = onboarding.build(hh_ok=True, resumes=resumes, directions=[], active=None, mode="", has_search=False)
    line = next(line for line in text.splitlines() if "2. Резюме" in line)
    if total:
        assert f"Резюме на hh.ru: {total} — " in line
    else:
        assert line.startswith("⬜️")
    for i in range(total):
        assert (f"«Резюме {i}, профессия {i}»" in line) == (i < 3)
    if total > 3:
        assert line.endswith(f" и ещё {total - 3}")
    else:
        assert "и ещё" not in line


def test_untitled_and_duplicate_resumes_count_as_entries():
    text, _, _ = onboarding.build(
        hh_ok=True, resumes=[{"title": "Резюме А"}, {"title": " "}, {"title": "Резюме А"}, {}, {}],
        directions=[], active=None, mode="", has_search=False,
    )
    assert 'Резюме на hh.ru: 5 — «Резюме А», «Резюме без названия», «Резюме А» и ещё 2' in text


@pytest.fixture
def getting_started_screen(tmp_path, monkeypatch):
    import directions
    from hh import session_probe
    from telegram_bot import TelegramBot

    history = tmp_path / "run_history.jsonl"
    profile = SimpleNamespace(home_dir=str(tmp_path), run_history_file=str(history),
                              hh=SimpleNamespace(cookies_file="synthetic-cookies", application_mode="auto"))
    bot = SimpleNamespace(
        _selected_profile=lambda principal: "synthetic-profile", _profile=lambda name: profile,
        _send_text=AsyncMock(),
    )
    probe = Mock()
    monkeypatch.setattr(session_probe, "probe", probe)
    monkeypatch.setattr(session_probe, "save_catalog", Mock())

    def render(records, *, missing=None, only_if_incomplete=False):
        history.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        resumes = [{"id": "synthetic-resume", "title": "Резюме А"}] if missing != 1 else []
        active = {"title": "Направление А", "application_mode": "confirm" if missing != 3 else "invalid"}
        if missing == 2:
            active = None
        probe.return_value = {"status": session_probe.OK if missing != 0 else session_probe.LOGIN,
                              "message": "", "resumes": resumes}
        monkeypatch.setattr(directions, "list_directions", lambda home: [active] if active else [])
        monkeypatch.setattr(directions, "active_direction", lambda home: active)
        bot._send_text.reset_mock()
        asyncio.run(TelegramBot._send_getting_started(bot, 42, {}, only_if_incomplete=only_if_incomplete))
        probe.assert_called_with("synthetic-cookies")
        return bot._send_text.call_args.args[1] if bot._send_text.called else None

    return render


def completed_search(**changes):
    return {"kind": "search", "mode": "search", "run_id": "synthetic-search",
            "status": "finished", "finished_at": "2026-10-23T12:00:00", "ok": True, **changes}


@pytest.mark.parametrize("missing", [None, 0, 1, 2, 3, 4])
def test_getting_started_reads_step_states_from_profile_and_probe(getting_started_screen, missing):
    text = getting_started_screen([completed_search()] if missing != 4 else [], missing=missing)
    # Failed login also makes the current HH resume catalog unavailable.
    for index in range(5):
        done = index != missing and not (missing == 0 and index == 1)
        assert f"{'✅' if done else '⬜️'} {index + 1}." in text
    assert ("Всё готово!" in text) == (missing is None)


@pytest.mark.parametrize("records,done", [
    pytest.param([], False, id="empty-history"),
    pytest.param([completed_search(kind="manual-apply", mode="manual-ai-apply")], False, id="manual-only"),
    pytest.param([completed_search(status="incomplete", finished_at=None, ok=None)], False, id="interrupted-first-search"),
    pytest.param([completed_search(ok=False, error_kind="CancelledError")], False, id="cancelled-first-search"),
    pytest.param([completed_search(ok=False, error_kind="RuntimeError")], False, id="failed-first-search"),
    pytest.param([completed_search(status="incomplete")], False, id="incomplete-overrides-ok"),
    pytest.param([completed_search(finished_at=None)], False, id="missing-finish-time"),
    pytest.param([{"run_id": "synthetic-unknown"}], False, id="run-id-alone"),
    pytest.param([completed_search(kind="manual-apply")], False, id="manual-kind-overrides-search-mode"),
    pytest.param([completed_search()], True, id="completed-search"),
    pytest.param([completed_search(mode="dry-run")], True, id="completed-trial-search"),
    pytest.param([completed_search(), completed_search(kind="manual-apply", mode="manual-ai-apply",
                                                      run_id="synthetic-manual")], True, id="search-then-manual"),
    pytest.param([completed_search(), completed_search(run_id="synthetic-interrupted", status="incomplete",
                                                      finished_at=None, ok=None)], True, id="search-then-interrupted"),
    pytest.param([{"kind": "search", "mode": "search", "run_id": "synthetic-legacy", "ok": True}],
                 True, id="legacy-terminal-search"),
    pytest.param([{"mode": "dry-run", "ok": True}], True, id="legacy-mode-only-search"),
])
@pytest.mark.parametrize("only_if_incomplete", [False, True])
def test_first_search_requires_completed_search_in_real_history(getting_started_screen, records, done, only_if_incomplete):
    text = getting_started_screen(records, only_if_incomplete=only_if_incomplete)
    if only_if_incomplete and done:
        assert text is None
    else:
        assert f"{'✅' if done else '⬜️'} 5. Первый поиск" in text
        assert ("Всё готово!" in text) == done
        assert ("Дальше бот присылает вакансии сам." in text) == done
