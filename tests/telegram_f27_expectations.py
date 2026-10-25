"""Reviewed F27 UX changes layered over immutable split-22 evidence.

Static path patches pin contextual actions, local summaries and launch drafts.
Original permission, write, queue and runner effects remain recorded evidence.
"""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path


GLOBAL_ROWS = [["🔎 Найти вакансии", "📋 Ждут решения"], ["📨 Отклики", "⚙️ Настройки"]]
RENAMES = (
    ("Нажмите «⏳ Выполняется...», чтобы обновить статус.", "Результат придёт отдельным сообщением."),
    ("🧭 Направления — выберите", "🎯 Мои поиски — выберите"),
    ("🎯 Направления", "🎯 Мои поиски"),
    ("🧭 Направления", "🧭 Мои поиски"),
    ("🧠 Обо мне", "🧠 Факты для ИИ"),
    ("Спрашивать меня перед откликом", "Подтверждать отклик"),
    ("Спрашивать меня", "Подтверждать отклик"),
    ("«Направления»", "«Мои поиски»"),
    ("3. Направления", "3. Мои поиски"),
    ("Что бот знает о вас · профиль", "🧠 Факты для ИИ · профиль"),
    ("«Откликнуться с ИИ»", "«Откликнуться»"),
    ("• Найти вакансии и Пробный поиск (без откликов) — в главном меню",
     "• Найти вакансии — кнопка внизу; Пробный поиск (без откликов) доступен перед запуском"),
    ("• Пробный поиск (без откликов) — в главном меню\n"
     "• Отклики — вакансии на проверку, ваши отклики, анкеты, ответы работодателям\n"
     "• Направления — что ищу, резюме, город, зарплата, расписание\n"
     "• hh.ru и резюме — вход на hh.ru и ваши резюме\n"
     "• Обо мне — что бот знает о вас\n"
     "• Состояние — работает ли бот, отклики, приглашения\n"
     "• С чего начать — настройка по шагам",
     "• Ждут решения — вакансии, которые требуют вашего решения\n"
     "• Отклики — отправленные отклики, приглашения и отказы\n"
     "• Настройки — Мои поиски, hh.ru, Факты для ИИ и расписание"),
)


def presentation(value):
    """Apply only the menu vocabulary and global reply-keyboard requirements."""
    if isinstance(value, dict):
        if "keyboard" in value:
            rows = value["keyboard"]
            flat = [button if isinstance(button, str) else button["text"] for row in rows for button in row]
            # The transient profile picker remains its own explicit selection.
            if not any(label.startswith("📁 Профиль:") for label in flat):
                layout = GLOBAL_ROWS if not flat or isinstance(rows[0][0], str) else [
                    [{"text": label} for label in row] for row in GLOBAL_ROWS]
                return {"keyboard": deepcopy(layout), "resize_keyboard": True, "is_persistent": True}
        return {key: presentation(item) for key, item in value.items()}
    if isinstance(value, list):
        return [presentation(item) for item in value]
    if isinstance(value, str):
        for old, new in RENAMES:
            value = value.replace(old, new)
    return value


def _without_navigation(state):
    """Only the selected screen and read-only search draft may change."""
    state = deepcopy(state)
    for user in state.get("user_state", {}).values():
        for name in ("menu", "updated_at", "find_launch"):
            user.pop(name, None)
    if "user_state" in state:
        state["user_state"] = {key: value for key, value in state["user_state"].items() if value}
        if not state["user_state"]:
            state.pop("user_state")
    return state


def _without_markup(value):
    if isinstance(value, dict):
        return {key: _without_markup(item) for key, item in value.items()
                if key not in {"markup", "reply_markup"}}
    if isinstance(value, list):
        return [_without_markup(item) for item in value]
    return value


@lru_cache(maxsize=1)
def _overlays():
    return json.loads((Path(__file__).with_name("fixtures") / "telegram_f27_overlays.json").read_text())


def f27_expected(kind, key, baseline):
    """Layer reviewed atomic UX deltas over immutable split-22 recordings.

    Every replacement checks its original value. Append operations keep all
    original messages and assert their count. Effects at process, authorization,
    queue, file and audit boundaries remain the original recorded expectations.
    """
    overlay = _overlays()
    expected = presentation(baseline)
    for patch_id in overlay[kind].get(key, []):
        path, before, after = overlay["patches"][patch_id]
        assert path[0] in {"replies", "texts", "state", "handlers", "resolved", "return"}, patch_id
        if path[0] == "texts":
            assert path[-1] == "reply_markup", patch_id
        target = expected
        for part in path[:-1]:
            target = target[part]
        last = path[-1]
        if last == "<append>":
            assert len(target) == before, (key, patch_id)
            target.extend(deepcopy(after))
        elif last == "<trim>":
            assert target[after:] == before, (key, patch_id)
            del target[after:]
        else:
            assert target[last] == before, (key, patch_id)
            target[last] = deepcopy(after)

    if kind == "dispatch":
        protected = ("settings_writes", "profile_writes", "direction_sessions", "queue", "documents")
        assert _without_navigation(expected["state"]) == _without_navigation(baseline["state"]), key
        if expected["resolved"] != baseline["resolved"]:
            assert (baseline["resolved"], expected["resolved"]) in (
                (["/search", ""], ["/find", ""]), (["⚙️", "Админ"], ["/menu_admin", ""])), key
        events = deepcopy(expected["handlers"])
        if expected["resolved"][0] == "/find" and events != baseline["handlers"]:
            assert baseline["handlers"] == [["_start_cli_command", ["qa-dev", "--search", "search", 3600], {}]], key
            assert events == [] and baseline["return"] is False and expected["return"] is None, key
        else:
            assert expected["return"] == baseline["return"], key
            for event in events:
                if event[0] == "_send_getting_started" and "cached" in event[2]:
                    assert event[2].pop("cached") is True, key
                if event[0] == "_show_review_queue" and "reset" in event[2]:
                    assert event[2].pop("reset") is True, key
            assert _without_markup(events) == _without_markup(baseline["handlers"]), key
    else:
        protected = ("return", "documents", "deleted", "audit", "cleared", "authorized", "captures",
                     "sessions", "auth", "downloads", "writes")
    for name in protected:
        if name == "documents" and name in baseline:
            assert _without_markup(expected[name]) == _without_markup(baseline[name]), (key, name)
        elif name in baseline:
            assert expected[name] == baseline[name], (key, name)
    return expected
