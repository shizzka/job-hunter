"""F15: owner choices and dispatch boundaries on one synthetic HH account."""
import asyncio
import copy
from pathlib import Path

import pytest

import apply_orchestrator
import config
import direction_runtime
import directions
import matcher
from tests.direction_view_fakes import two_directions
from tests.test_telegram_directions import Harness, USER


def test_migration_is_an_explicit_saved_direction_choice(tmp_path):
    bot = Harness(tmp_path)
    home = Path(bot.profiles["client_1"].home_dir)
    two_directions(home)
    letter = "Здравствуйте! Проверяю программы вручную."
    (home / "cover_letter_fallback.md").write_text(letter)

    async def choose():
        await bot._dispatch_direction(1, USER, "/direction_edit", "cover_letter_fallback")
        state = bot._direction_sessions()[1]
        assert "Посмотреть прежнее письмо" in str(bot.sent[-1])
        assert "cover_letter_fallback" not in directions.active_direction(home)
        await bot._direction_callback(1, USER, f"dir:{state['token']}:letteroffer")
        assert letter in bot.sent[-1][1] and "QA Engineer" in bot.sent[-1][1]
        assert "cover_letter_fallback" not in state["draft"]
        await bot._direction_callback(1, USER, f"dir:{state['token']}:letteruse")
        assert state["draft"]["cover_letter_fallback"] == letter
        assert "cover_letter_fallback" not in directions.active_direction(home)
        await bot._dispatch_direction(1, USER, "/direction_save", "")
    asyncio.run(choose())
    assert directions.active_direction(home)["cover_letter_fallback"] == letter
    assert "cover_letter_fallback" not in directions.load_direction(home, "electric")
    assert (home / "cover_letter_fallback.md").read_text() == letter


@pytest.mark.parametrize("text", ["", "  ", "x" * 1501, "Письмо\t", "Письмо\r\n", "Письмо\x00"])
def test_direction_letter_uses_existing_validation(tmp_path, text):
    two_directions(tmp_path)
    value = directions.active_direction(tmp_path)
    value["cover_letter_fallback"] = text
    if text.strip():
        with pytest.raises(ValueError):
            directions.save_direction(tmp_path, value)
    # An absent fallback is valid on disk; the text editor requires actual text.
    from fallback_letter import validate
    with pytest.raises(ValueError):
        validate(text)


def test_edit_cancel_and_stale_migration_offer(tmp_path):
    bot = Harness(tmp_path)
    home = Path(bot.profiles["client_1"].home_dir)
    two_directions(home)
    (home / "cover_letter_fallback.md").write_text("Прежнее письмо QA.")

    async def edit():
        await bot._dispatch_direction(1, USER, "/direction_edit", "cover_letter_fallback")
        state = bot._direction_sessions()[1]
        await bot._accept_direction_input(1, USER, "x" * 1501)
        assert state["mode"] == "edit" and "1500" in bot.sent[-1][1]
        await bot._accept_direction_input(1, USER, "Моё новое письмо.\nВторая строка.")
        assert "Моё новое письмо" in bot.sent[-1][1]
        await bot._dispatch_direction(1, USER, "/direction_cancel", "")
        assert "cover_letter_fallback" not in directions.active_direction(home)
        await bot._dispatch_direction(1, USER, "/direction_edit", "cover_letter_fallback")
        state = bot._direction_sessions()[1]
        await bot._direction_callback(1, USER, f"dir:{state['token']}:letteroffer")
        directions.set_active_direction(home, "electric")
        await bot._direction_callback(1, USER, f"dir:{state['token']}:letteruse")
        assert "устарела" in bot.sent[-1][1]
        assert "cover_letter_fallback" not in directions.active_direction(home)
    asyncio.run(edit())


def test_letters_follow_application_resume_at_dispatch(tmp_path, monkeypatch):
    two_directions(tmp_path)
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(config, "HH_APPLY_TRACE_ENABLED", False)
    monkeypatch.setattr(config, "ANALYTICS_ENABLED", False)
    monkeypatch.setattr(apply_orchestrator.company_blacklist, "is_blocked", lambda _: False)
    (tmp_path / "cover_letter_fallback.md").write_text("Чужое письмо профиля QA.")
    sent = []
    async def dispatch(vacancy, cover, *args, **kwargs):
        sent.append((cover, kwargs["preferred_resume_id"]))
        return {"ok": False, "message": "Synthetic endpoint"}
    monkeypatch.setattr(apply_orchestrator, "_dispatch_apply", dispatch)
    async def fallback(vacancy, details=""):
        return matcher._fallback_cover_letter(vacancy)
    monkeypatch.setattr(matcher, "_generate_cover_letter", fallback)
    bindings = {}
    for name, letter in (("qa", "Проверяю программы."), ("electric", "Монтирую проводку.")):
        value = directions.load_direction(tmp_path, name)
        value["cover_letter_fallback"] = letter
        value = directions.save_direction(tmp_path, value)
        directions.set_active_direction(tmp_path, name)
        monkeypatch.setattr(config, "ACTIVE_DIRECTION", value, raising=False)
        vacancy = {"id": "1", "source": "hh", "_hh_work_conditions": {"formats": ["remote"], "fly_in_fly_out": False}}
        cover = asyncio.run(matcher.generate_cover_letter(vacancy))
        assert cover == letter
        bindings[name] = copy.deepcopy(vacancy)
        asyncio.run(apply_orchestrator.dispatch_apply(vacancy, cover, preferred_resume_id="wrong-resume"))
    assert sent == [("Проверяю программы.", "resume_111"), ("Монтирую проводку.", "resume_222")]
    # A swapped letter, provenance, queue selection or active direction stops before the client.
    vacancy = bindings["electric"]
    for changed in ("text", "binding", "direction"):
        bad = copy.deepcopy(vacancy)
        letter = "Проверяю программы." if changed == "text" else "Монтирую проводку."
        if changed == "binding":
            bad["_cover_letter_selection"] = bindings["qa"]["_cover_letter_selection"]
        elif changed == "direction":
            bad["_direction_selection"] = bindings["qa"]["_direction_selection"]
        result = asyncio.run(apply_orchestrator.dispatch_apply(bad, letter))
        assert result["reason"] == "direction_variant_unavailable"
    assert len(sent) == 2
    # Two hashes claiming the same HH resume number are not two professions.
    qa = directions.load_direction(tmp_path, "qa")
    qa["variants"][0]["hh_resume_numeric_id"] = 222
    directions.save_direction(tmp_path, qa)
    result = asyncio.run(apply_orchestrator.dispatch_apply(vacancy, "Монтирую проводку."))
    assert result["reason"] == "direction_variant_unavailable" and len(sent) == 2


def test_missing_direction_letter_keeps_generic_and_single_direction_uses_own(tmp_path, monkeypatch):
    from tests.direction_view_fakes import make_direction
    value = make_direction(tmp_path)
    directions.set_active_direction(tmp_path, "qa")
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", value, raising=False)
    (tmp_path / "cover_letter_fallback.md").write_text("Старое письмо.")
    assert "Старое письмо" not in matcher._fallback_cover_letter({"id": "1"})
    value["cover_letter_fallback"] = "Письмо выбранного направления."
    assert matcher._fallback_cover_letter({"id": "1"}) == value["cover_letter_fallback"]
