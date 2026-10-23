"""Profile-bound, confirmed Telegram direction drafts.

Raw HH exports stay in memory until Save; every action rechecks the selected
profile and the exact base direction, including async completion.
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
import hashlib
import html
import json
from pathlib import Path
import re
import uuid

import search_query_suggester
import work_conditions
import profile_access
import telegram_access
from telegram_app.guards import read_command, read_thread


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


STRATEGY_BUTTONS = {
    "single": "Всегда первое",
    "closest": "Самое подходящее",
    "ab": "По очереди",
}
STRATEGY_HELP = (
    "• Всегда первое — все отклики одним резюме, первым включённым в списке.\n"
    "• Самое подходящее — бот сравнивает вакансию с каждым резюме и берёт самое похожее.\n"
    "• По очереди — резюме чередуются, через пару недель видно, какое работает лучше."
)
STRATEGY_LABELS = {
    "single": "Всегда первое",
    "closest": "Самое подходящее",
    "ab": "По очереди",
}


def counted(value, forms):
    number = int(value or 0)
    word = forms[2] if 11 <= number % 100 <= 14 else forms[0] if number % 10 == 1 else forms[1] if 2 <= number % 10 <= 4 else forms[2]
    return f"{number} {word}"


def resume_title(variant):
    return str(variant.get("title") or "Резюме без названия")


def resume_state(variant):
    return "включено" if variant.get("status", "active") == "active" else "выключено"


def parse_selection(text: str, count: int) -> list[int]:
    values = re.split(r"[\s,;]+", text.strip())
    if not values or any(not value.isdigit() for value in values):
        raise ValueError("Отправьте один или несколько номеров через пробел или запятую.")
    selected = list(dict.fromkeys(int(value) - 1 for value in values))
    if any(value < 0 or value >= count for value in selected):
        raise ValueError("Выберите номера из показанного списка.")
    return selected


class TelegramDirections:
    def _direction_sessions(self):
        if not hasattr(self, "_direction_pending"):
            self._direction_pending = {}
        return self._direction_pending

    def _direction_context(self, principal):
        import directions
        profile_name = self._selected_profile(principal)
        profile = self._profile(profile_name)
        active = directions.active_direction(profile.home_dir)
        return profile_name, profile, active

    def _direction_bind(self, principal, **fields):
        profile_name, profile, active = self._direction_context(principal)
        state = {"profile_name": profile_name, "home_dir": str(profile.home_dir),
                 "base": _fingerprint(active), "token": uuid.uuid4().hex[:12], **fields}
        self._direction_sessions()[principal["user_id"]] = state
        return state

    def _direction_valid(self, principal, state):
        profile_name, profile, active = self._direction_context(principal)
        return (state.get("profile_name") == profile_name and state.get("home_dir") == str(profile.home_dir)
                and state.get("base") == _fingerprint(active))

    def _direction_current(self, principal, state):
        return (self._direction_sessions().get(principal["user_id"]) is state
                and self._direction_valid(principal, state))

    async def _request_application_mode(self, chat_id, principal, mode):
        """All mode entry points request auto; only its bound Yes callback saves it."""
        from telegram_bot_ui import MENU_APPLICATION_MODE, reply_menu_for_command
        if mode not in {"preview", "confirm", "auto"}:
            raise ValueError("Неизвестный режим откликов.")
        profile_name, profile, active = self._direction_context(principal)
        menu = reply_menu_for_command(principal.get("role", "user"), "/search_application_" + mode) or MENU_APPLICATION_MODE
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return
        if mode == "auto":
            state = self._direction_bind(principal, mode="application_auto",
                base_mode=getattr(profile.hh, "application_mode", None), menu=menu)
            await self._send_text(chat_id, "🛡 Режим откликов",
                reply_markup=self._menu_reply_markup(principal, menu=menu))
            scope = (f"направления «{active['title']}»" if active else f"профиля «{profile_name}»")
            await self._send_text(chat_id,
                f"Бот будет сам откликаться на подходящие вакансии {scope}. Включить?",
                reply_markup=self._direction_inline(state,
                    [[("Да, включить", "autoyes"), ("Нет", "autono")]]))
            return
        self._direction_sessions().pop(principal["user_id"], None)
        await self._save_application_mode(chat_id, principal, mode, menu=menu)

    async def _save_application_mode(self, chat_id, principal, mode, *, menu):
        import directions
        profile_name, profile, active = self._direction_context(principal)
        scope = f"по направлению «{active['title']}»" if active else f"в профиле «{profile_name}»"
        message = {"confirm": f"Сохранено: {scope} бот сначала присылает вакансию вам и откликается только после вашего «да».",
                   "preview": f"Сохранено: {scope} бот только показывает вакансии без откликов.",
                   "auto": f"Сохранено: {scope} бот теперь откликается сам."}[mode]
        if not active:
            await self._save_search_setting(chat_id, principal, {"HH_APPLICATION_MODE": mode},
                                            message, menu=menu)
            return
        draft = copy.deepcopy(active)
        draft["application_mode"] = mode
        try:
            directions.save_direction(profile.home_dir, draft, texts={})
        except (ValueError, OSError) as exc:
            await self._send_text(chat_id, f"❌ Не удалось сохранить режим: {exc}",
                reply_markup=self._menu_reply_markup(principal, menu=menu))
            # A storage error may follow a committed write; retain its claim.
            return False if isinstance(exc, ValueError) else None
        result = f"✅ {message}"
        try:
            self._reload_profile_daemon(profile_name)
        except RuntimeError as exc:
            result = (f"⚠️ Настройки сохранены, но повтор не перезапустился: {exc}\n"
                      f"{message}\n"
                      "Новый режим может ещё не действовать. Проверьте статус.")
        await self._send_text(chat_id, result,
            reply_markup=self._menu_reply_markup(principal, menu=menu))

    def _direction_markup(self, *, draft=False):
        rows = ([["✅ Сохранить направление", "✏️ Править направление"], ["❌ Отменить направление"]] if draft else
                # Actions live on inline buttons of the cards; the reply keyboard stays minimal.
                [])
        rows.append(["🧭 Направления", "🏠 Главное"])
        return {"keyboard": [[{"text": label} for label in row] for row in rows], "resize_keyboard": True}

    async def _ask_choose_direction(self, chat_id, principal, text):
        """A request to pick a direction comes with the direction buttons themselves."""
        import directions
        profile_name, profile, active = self._direction_context(principal)
        choices = directions.list_directions(profile.home_dir)
        state = self._direction_bind(principal, mode="switch", choices=choices)
        rows = [[(str(item["title"])[:60], f"choose:{i}")] for i, item in enumerate(choices)]
        rows.append([("➕ Новое направление", "new")])
        await self._send_text(chat_id, text if choices else "Направлений пока нет — создайте первое.",
                              reply_markup=self._direction_inline(state, rows))

    def _direction_inline(self, state, rows):
        # Each rendered keyboard is a new revision of this draft. A repeat of
        # an old revision stays spent; the freshly rendered checkbox is usable.
        state["token"] = uuid.uuid4().hex[:12]
        return {"inline_keyboard": [[{"text": label, "callback_data": f"dir:{state['token']}:{action}"}
                                      for label, action in row] for row in rows]}

    async def _direction_show_catalog(self, chat_id, state):
        selected = state.setdefault("selected", [])
        rows = [[(("✅ " if i in selected else "☐ ") + resume_title(item))[:90], f"toggle:{i}"]
                for i, item in enumerate(state["catalog"])]
        markup = self._direction_inline(state, [[tuple(row)] for row in rows] +
                                        [[("Далее →", "next")], [("Отмена", "cancel")]])
        await self._send_text(chat_id, "Выберите резюме кнопками, затем нажмите «Далее». Тексты будут загружены с hh.ru.", reply_markup=markup)

    async def _direction_show_draft(self, chat_id, principal, state):
        draft = state.get("draft") or {}
        if state.get("activate"):
            _, _, active = self._direction_context(principal)
            current = (active or {}).get("title", "")
            text = (f"Искать по направлению «{draft.get('title', '')}» вместо «{current}»?" if current else
                    f"Искать по направлению «{draft.get('title', '')}»?")
            rows = [[("Да, переключить", "save")], [("Назад", "list"), (f"Нет, оставить «{current}»" if current else "Не переключать", "cancel")]]
        else:
            variants = "\n".join(f"• {resume_title(v)} · {resume_state(v)}" for v in draft.get("variants", []))
            profiles = draft.get("search_profiles", [])
            cities = {"1": "Москва", "2": "Санкт-Петербург", "113": "Россия"}
            formats = {"": "любой формат", "remote": "удалённо", "fullDay": "в офисе", "flyInFlyOut": "вахта", "shift": "сменный", "flexible": "гибкий"}
            profile_labels = [f"{cities.get(str(x['area']), str(x['area']))} / {formats.get(x['schedule'], x['schedule'])}" for x in profiles]
            conditions = draft.get("work_conditions", work_conditions.defaults(draft))
            allowed = ", ".join(work_conditions.FORMAT_LABELS[f] for f in conditions["allowed_formats"])
            strategy = STRATEGY_LABELS.get(draft.get('selection_strategy', 'single'), STRATEGY_LABELS['single'])
            text = (f"Направление «{draft.get('title', '')}»\n"
                    f"Каким резюме откликаться: {strategy}.\n\n"
                    f"Ищу по словам: {', '.join(draft.get('search_queries', [])) or 'не указаны'}\n"
                    f"Должности: {', '.join(draft.get('goal', [])) or 'не указаны'}\n"
                    f"Формат: {allowed}. Вахта — {'да' if conditions['allow_fly_in_fly_out'] else 'нет'}.\n"
                    "Город и формат — " + (", ".join(profile_labels) or "из профиля") + ".\n"
                    f"Города работы — {', '.join(conditions.get('allowed_cities', [])) or 'любые'}.\n"
                    f"Зарплата: {str(draft['salary_baseline']) + ' ₽' if draft.get('salary_baseline') else 'не указана'}\n\n"
                    "Резюме:\n" + variants)
            text += "\n\nЗапасное сопроводительное письмо: " + (draft.get("cover_letter_fallback") or "не задано, отправляется общее короткое письмо.")
            text += "\n\nОписание: " + (draft.get('positioning') or 'не задано')
            if not state.get("summary"):
                _, _, current = self._direction_context(principal)
                # Describe the existing selection draft without changing its save semantics.
                selected = [v for v in draft.get("variants", []) if v.get("status", "active") == "active"]
                if current and draft.get("selection_strategy") == "single" and selected:
                    text = (f"Не сохранено: откликаться только резюме «{resume_title(selected[0])}»\n"
                            f"Направление «{draft.get('title', '')}». Сейчас: {STRATEGY_LABELS.get(current.get('selection_strategy', 'single'))}.\n"
                            f"После сохранения все отклики пойдут резюме «{resume_title(selected[0])}». "
                            "Другие резюме останутся в списке, но использоваться не будут.\n\n" + text)
                else:
                    text = "Не сохранено\n" + text
            rows = [[("✏️ Изменить условия", "editmenu"), (f"🧾 Резюме ({len(draft.get('variants', []))})", "variants")],
                    [("🔀 Каким резюме откликаться", "strategy"), ("📊 Статистика резюме", "stats")]]
            if not state.get("summary"):
                rows += [[("✅ Сохранить", "save"), ("Не сохранять", "cancel")]]
                text += "\n\nИзменение вступит в силу после кнопки «Сохранить»."
            rows += [[("Все направления", "list")]]
            if state.get("summary"):
                rows += [[("Удалить направление…", "delete")]]
        await self._send_text(chat_id, str(text), reply_markup=self._direction_inline(state, rows))

    async def _direction_edit_menu(self, chat_id, state):
        labels = [("Название", "title"), ("Описание", "positioning"), ("Должности", "goal"),
                  ("Ищу по словам", "queries"), ("Город", "city"), ("Формат поиска", "format"), ("Города и форматы", "search_profiles"),
                  ("Допустимые форматы работы", "work_formats"), ("Разрешена ли вахта", "fly_allowed"),
                  ("Города места работы", "work_cities"),
                  ("Запасное сопроводительное письмо", "cover_letter_fallback"),
                  ("Зарплата", "salary"), ("Подходящие слова", "relevant"), ("Исключения", "exclude")]
        await self._send_text(chat_id, "Что изменить?", reply_markup=self._direction_inline(state,
            [[(label, f"edit:{field}")] for label, field in labels] + [[("Назад", "back"), ("Отмена", "cancel")]]))

    @telegram_access.scoped_request
    async def _direction_callback(self, chat_id, principal, data):
        parts = str(data).split(":")
        state = self._direction_sessions().get(principal["user_id"])
        if len(parts) < 3 or not state or parts[1] != state.get("token") or not self._direction_current(principal, state):
            await self._send_text(chat_id, "Эта кнопка устарела. Откройте «Направления» заново.")
            return False
        action = parts[2]
        started = False
        try:
            if action in {"autoyes", "autono"} and state.get("mode") == "application_auto":
                if action == "autono":
                    started = True
                    self._direction_sessions().pop(principal["user_id"], None)
                    await self._send_text(chat_id, "Автоматические отклики не включены. Настройки не менялись.",
                        reply_markup=self._menu_reply_markup(principal, menu=state["menu"]))
                    return
                profile_name, profile, _ = self._direction_context(principal)
                if state.get("base_mode") != getattr(profile.hh, "application_mode", None):
                    await self._send_text(chat_id, "Режим изменился. Выберите его заново.")
                    return False
                if self._has_active_command(profile_name):
                    await self._send_busy_status(chat_id, principal, profile_name=profile_name)
                    return False
                started = True
                result = await self._save_application_mode(chat_id, principal, "auto", menu=state["menu"])
                if result is not False and self._direction_sessions().get(principal["user_id"]) is state:
                    self._direction_sessions().pop(principal["user_id"], None)
                return result
            elif action == "toggle" and state.get("mode") == "catalog":
                i = int(parts[3])
                if not 0 <= i < len(state["catalog"]):
                    raise ValueError("Резюме не найдено.")
                selected = state.setdefault("selected", [])
                started = True
                selected.remove(i) if i in selected else selected.append(i)
                await self._direction_show_catalog(chat_id, state)
            elif action == "next" and state.get("mode") == "catalog":
                selected = state.get("selected", [])
                if not selected:
                    raise ValueError("Выберите хотя бы одно резюме.")
                started = True
                state["mode"] = "working"
                result = await self._direction_job(chat_id, principal, state, "add" if state.get("add") else "new",
                                                   [state["catalog"][i] for i in sorted(selected)])
                if result is False:
                    state["mode"] = "catalog"
                return result
            elif action == "choose" and state.get("mode") == "switch":
                item = state["choices"][int(parts[3])]
                profile_access.require_direction(item["name"])
                _, _, active = self._direction_context(principal)
                started = True
                state.update(mode="draft", draft=copy.deepcopy(item), activate=not active or item["name"] != active["name"],
                             summary=bool(active and item["name"] == active["name"]))
                await self._direction_show_draft(chat_id, principal, state)
            elif action == "statsrefresh":
                started = True
                return await self._dispatch_direction(chat_id, principal, "/direction_stats_refresh", "")
            elif action == "editmenu":
                await self._direction_edit_menu(chat_id, state)
            elif action == "edit":
                await self._dispatch_direction(chat_id, principal, "/direction_edit", parts[3])
            elif action == "letteroffer" and state.get("field") == "cover_letter_fallback":
                from fallback_letter import legacy_offer
                letter = legacy_offer(state["home_dir"])
                if not letter:
                    raise ValueError("Сохранённого письма нет или его нужно исправить.")
                state["letter_offer"] = letter
                await self._send_text(chat_id,
                    f"Ранее сохранённое письмо:\n\n{letter}\n\n"
                    f"Использовать это письмо для направления «{state['draft']['title']}»? "
                    "Выберите его, только если опыт и профессия совпадают.",
                    reply_markup=self._direction_inline(state, [[("Да, добавить к изменениям", "letteruse")], [("Назад", "back")]]))
            elif action == "letteruse" and state.get("letter_offer") and state.get("mode") == "edit" and state.get("field") == "cover_letter_fallback":
                from fallback_letter import validate
                started = True
                state["draft"]["cover_letter_fallback"] = validate(state.pop("letter_offer"))
                state.update(mode="draft", summary=False)
                await self._direction_show_draft(chat_id, principal, state)
            elif action == "letterclear" and state.get("mode") == "edit" and state.get("field") == "cover_letter_fallback":
                started = True
                state["draft"]["cover_letter_fallback"] = ""
                state.update(mode="draft", summary=False)
                await self._direction_show_draft(chat_id, principal, state)
            elif action == "preset" and state.get("mode") == "edit":
                values = {"moscow": "1", "spb": "2", "russia": "113", "remote": "remote", "office": "fullDay", "fly": "flyInFlyOut", "any": "любой"}
                value = values[parts[3]]
                started = True
                await self._accept_direction_input(chat_id, principal, value)
            elif action == "conditionpreset" and state.get("mode") == "edit":
                values = {"yes": "да", "no": "нет", "remote": "удалённо, гибрид", "any": "любой"}
                value = values[parts[3]]
                started = True
                await self._accept_direction_input(chat_id, principal, value)
            elif action == "pair" and state.get("draft"):
                pairs = {"spb": {"area": "2", "schedule": ""}, "fly": {"area": "113", "schedule": "flyInFlyOut"}}
                pair = pairs[parts[3]]
                started = True
                rows = state["draft"].setdefault("search_profiles", [])
                if pair not in rows:
                    rows.append(pair)
                state.update(mode="draft", summary=False)
                await self._direction_show_draft(chat_id, principal, state)
            elif action == "back":
                state["mode"] = "draft"
                await self._direction_show_draft(chat_id, principal, state)
            elif action == "strategy":
                current = state["draft"].get("selection_strategy", "single")
                rows = [[(("✓ " if key == current else "") + STRATEGY_BUTTONS[key], f"strategyset:{key}")]
                        for key in ("single", "closest", "ab")]
                rows.append([("Назад", "back")])
                text = (f"Каким резюме откликаться в направлении «{state['draft'].get('title', '')}»?\n"
                        f"Сейчас: {STRATEGY_LABELS[current].lower()}.\n\n" + STRATEGY_HELP + "\n\n" +
                        ("Выбор сохраняется сразу после нажатия." if state.get("summary") else "Изменение вступит в силу после кнопки «Сохранить»."))
                await self._send_text(chat_id, text, reply_markup=self._direction_inline(state, rows))
            elif action == "strategyset" and len(parts) > 3 and parts[3] in STRATEGY_LABELS:
                saved_direction = bool(state.get("summary"))
                if saved_direction and self._has_active_command(state["profile_name"]):
                    await self._send_busy_status(chat_id, principal, profile_name=state["profile_name"])
                    return False
                started = True
                state["draft"]["selection_strategy"] = parts[3]
                state.update(mode="draft", summary=False)
                if saved_direction:
                    # One explicit choice on an existing direction: save it right away.
                    await self._send_text(chat_id, "Выбор резюме: " + STRATEGY_LABELS[parts[3]] + ". Сохраняю…")
                    return await self._dispatch_direction(chat_id, principal, "/direction_save", "")
                else:
                    await self._direction_show_draft(chat_id, principal, state)
            elif action == "delete":
                state["delete_target"] = copy.deepcopy(state["draft"])
                await self._send_text(chat_id, "Удалить направление?", reply_markup=self._direction_inline(state,
                    [[("Да, удалить", "deleteyes"), ("Назад", "back")]]))
            elif action == "deleteyes" and state.get("delete_target"):
                import directions
                if self._has_active_command(state["profile_name"]):
                    await self._send_busy_status(chat_id, principal, profile_name=state["profile_name"])
                    return False
                target = state["delete_target"]
                if directions.load_direction(state["home_dir"], target["name"]) != target:
                    raise ValueError("Направление изменилось; откройте список заново.")
                started = True
                directions.delete_direction(state["home_dir"], target["name"], expected=target)
                self._reload_profile_daemon(state["profile_name"])
                await self._dispatch_direction(chat_id, principal, "/directions", "")
            elif action in {"list", "new", "save", "cancel", "variants", "stats", "add", "pause", "resume", "remove", "refresh"}:
                command = {"list": "/directions", "new": "/direction_new", "save": "/direction_save", "cancel": "/direction_cancel",
                           "variants": "/direction_variants", "stats": "/direction_stats"}.get(action, "/direction_" + action)
                handler = self._read_direction if action in {"list", "stats"} else self._dispatch_direction
                # A delegated action may mutate storage before its delivery fails.
                # Only an explicit False from it proves that nothing started.
                started = True
                return await handler(chat_id, principal, command, "")
            elif action == "variant" and state.get("mode") == "variant":
                started = True
                await self._accept_direction_input(chat_id, principal, str(int(parts[3]) + 1))
            else:
                return False
        except (ValueError, IndexError, KeyError, OSError) as exc:
            await self._send_text(chat_id, str(str(exc)))
            return None if started else False

    async def _direction_prompt_variants(self, chat_id, principal, operation):
        _, _, active = self._direction_context(principal)
        pending = self._direction_sessions().get(principal["user_id"])
        if pending and self._direction_current(principal, pending) and pending.get("draft") and not pending.get("activate"):
            state = pending
            active = state["draft"]
            state.update(mode="variant", operation=operation)
        elif active:
            state = self._direction_bind(principal, mode="variant", operation=operation, draft=copy.deepcopy(active), texts={})
        else:
            await self._ask_choose_direction(chat_id, principal, "Сначала выберите направление:")
            return
        variants = active["variants"]
        enabled = sum(v.get("status", "active") == "active" for v in variants)
        status = "все включены" if enabled == len(variants) else f"включено {enabled}, выключено {len(variants) - enabled}"
        lines = [f"Резюме направления «{active['title']}»: {len(variants)}, {status}"]
        for index, variant in enumerate(variants, 1):
            lines.append(f"{index}. {resume_title(variant)} · {resume_state(variant)}")
        prompts = {"select": "Нажмите на резюме, чтобы откликаться только им.",
                   "pause": "Какое резюме выключить? Нажмите на него.",
                   "resume": "Какое резюме включить? Нажмите на него.",
                   "remove": "Какое резюме убрать из направления? Нажмите на него."}
        lines += ["", prompts[operation], "Изменение вступит в силу после кнопки «Сохранить»."]
        rows = [[((f"Только «{resume_title(v)}»" if operation == "select" else resume_title(v)), f"variant:{i}")] for i, v in enumerate(variants)]
        rows += [[("Добавить резюме", "add"), ("Выключить резюме…", "pause"), ("Включить резюме…", "resume")],
                 [("Убрать резюме из направления…", "remove"), ("Обновить резюме с hh.ru", "refresh")], [("Назад", "back"), ("Не сохранять", "cancel")]]
        await self._send_text(chat_id, "\n".join(lines), reply_markup=self._direction_inline(state, rows))

    async def _direction_catalog(self, chat_id, principal, *, add=False):
        profile_name, profile, active = self._direction_context(principal)
        pending = self._direction_sessions().get(principal["user_id"])
        texts = {}
        if add and pending and self._direction_current(principal, pending) and pending.get("draft") and not pending.get("activate"):
            active = pending["draft"]
            texts = dict(pending.get("texts") or {})
        if add and not active:
            await self._ask_choose_direction(chat_id, principal, "Сначала выберите направление:")
            return
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return False
        state = self._direction_bind(principal, mode="working", add=add,
                                     draft=copy.deepcopy(active) if add else None, texts=texts)
        command = self._mark_active_command(principal=principal, label="direction catalog", profile_name=profile_name)
        await self._send_text(chat_id, "Загружаю список ваших резюме с hh.ru…")

        async def runner():
            try:
                # The legacy QA catalog can omit other professions. Read the
                # authenticated catalog without title filters or changing HH.
                if not self._direction_current(principal, state):
                    raise ValueError("Профиль, направление или ваши изменения уже другие; загрузка отменена.")
                from hh.session_probe import probe, OK
                checked = await asyncio.to_thread(probe, profile.hh.cookies_file)
                if checked["status"] != OK:
                    raise ValueError(checked["message"] + " Откройте «🔐 hh.ru и резюме» → «Вход в hh.ru», затем повторите.")
                catalog = await self._direction_load_catalog(profile, chat_id=chat_id)
                if not self._direction_current(principal, state):
                    raise ValueError("Профиль, направление или ваши изменения уже другие; загрузка отменена.")
                if not catalog:
                    raise ValueError("На hh.ru в вашем аккаунте нет резюме. Создайте резюме на сайте hh.ru и повторите.")
                state.update(mode="catalog", catalog=copy.deepcopy(catalog))
                await self._direction_show_catalog(chat_id, state)
                command.result = {"ok": True}
            except Exception as exc:
                command.result = {"ok": False}
                await self._send_text(chat_id, str(f"Не удалось прочитать резюме: {exc}"))
            finally:
                self._clear_active_command(profile_name)
        command.task = self._create_active_task(command, runner)

    @asynccontextmanager
    async def _direction_hh_client(self, profile, *, chat_id):
        from hh_client import HHClient
        from hh.browser import CookiePaths
        import notifier
        client = HHClient()
        client._cookie_paths = CookiePaths(str(Path(profile.hh.cookies_file).resolve()), str(Path(profile.home_dir) / "hh_state"))
        client._ui_home = str(profile.home_dir)
        client._ui_target = (notifier._resolve_bot_token(profile), (chat_id,), notifier._resolve_proxy_url(profile))
        try:
            await client.start()
            yield client
        finally:
            if await client.stop(persist_cookies=False) is False:
                raise RuntimeError("Не удалось завершить сессию скачивания HH.")

    async def _direction_load_catalog(self, profile, *, chat_id):
        async with self._direction_hh_client(profile, chat_id=chat_id) as client:
            return await client.get_resume_ids_readonly()

    async def _direction_download(self, profile, items, *, chat_id):
        async with self._direction_hh_client(profile, chat_id=chat_id) as client:
            # Verify chosen hashes still belong to this authenticated account.
            available = {str(item["id"]): item for item in await client.get_resume_ids_readonly()}
            result = []
            for item in items:
                key = str(item.get("hh_resume_hash") or item.get("id") or "")
                if key not in available:
                    raise ValueError("Выбранное резюме не найдено в текущей HH-сессии. Обновите каталог через вход HH.")
                data = await client.download_resume_readonly(available[key])
                if data.get("hh_resume_hash", key) != key:
                    raise ValueError("HH вернул другое резюме; скачивание остановлено.")
                identities = {v for v in (data.get("hh_resume_numeric_id"), available[key].get("hh_resume_numeric_id"),
                                          item.get("hh_resume_numeric_id")) if v is not None}
                if len(identities) > 1:
                    raise ValueError("Числовой ID резюме HH изменился; обновите каталог.")
                data["hh_resume_numeric_id"] = next(iter(identities), None)
                if not str(data.get("raw") or "").strip():
                    raise ValueError("HH вернул пустой текст резюме.")
                result.append({**data, "hh_resume_hash": key, "title": data.get("title") or available[key].get("title") or key})
            return result

    async def _direction_job(self, chat_id, principal, state, operation, items=None):
        profile_name = state["profile_name"]
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return False
        ai = operation in {"new", "suggest"}
        if ai and not await self._authorize_ai_usage(chat_id, principal, profile_name=profile_name,
                                                    action="direction_suggest_ai_used", check_only=True):
            return False
        active_command = self._mark_active_command(principal=principal, label="direction draft", profile_name=profile_name)
        await self._send_text(chat_id, "Готовлю направление для проверки. Дождитесь результата; изменения потребуют сохранения.")

        async def runner():
            try:
                if not self._direction_current(principal, state):
                    raise ValueError("Профиль или активное направление изменились. Откройте /directions заново.")
                profile = self._profile(profile_name)
                draft = copy.deepcopy(state.get("draft"))
                texts = dict(state.get("texts") or {})
                if operation in {"new", "add", "refresh"}:
                    exports = await self._direction_download(profile, items or [], chat_id=chat_id)
                    if draft is None:
                        draft = {"name": "direction_" + uuid.uuid4().hex[:12], "variants": [], "selection_strategy": "single"}
                    for exported in exports:
                        key = exported["hh_resume_hash"]
                        texts[key] = exported["raw"]
                        old = next((v for v in draft["variants"] if v["hh_resume_hash"] == key), None)
                        variant = {"hh_resume_hash": key, "hh_resume_numeric_id": exported.get("hh_resume_numeric_id"),
                                   "title": exported["title"], "status": (old or {}).get("status", "active"),
                                   "tags": (old or {}).get("tags", [])}
                        if exported.get("fetched_at"):
                            variant["fetched_at"] = exported["fetched_at"]
                        if old:
                            old.clear()
                            old.update(variant)
                        else:
                            draft["variants"].append(variant)
                if ai:
                    if not self._direction_current(principal, state):
                        raise ValueError("Профиль или направление изменились; изменения не сохранены.")
                    if not await self._authorize_ai_usage(chat_id, principal, profile_name=profile_name,
                                                         action="direction_suggest_ai_used"):
                        return
                    if not self._direction_current(principal, state):
                        raise ValueError("Профиль, направление или ваши изменения уже другие; проверка отменена.")
                    resumes = [{"title": v["title"], "raw": texts.get(v["hh_resume_hash"]) or Path(v["text_file"]).read_text(encoding="utf-8")}
                               for v in draft["variants"] if v.get("status", "active") == "active"]
                    suggestion = await search_query_suggester.suggest_direction(resumes=resumes, current_direction=state.get("draft"), profile_dir=str(profile.home_dir))
                    draft.update(suggestion)
                if not self._direction_current(principal, state):
                    raise ValueError("Профиль, направление или ваши изменения уже другие; результат отменён.")
                state.update(mode="draft", draft=draft, texts=texts)
                await self._direction_show_draft(chat_id, principal, state)
                active_command.result = {"ok": True}
            except Exception as exc:
                active_command.result = {"ok": False}
                await self._send_text(chat_id, str(f"Не удалось подготовить направление: {exc}"))
            finally:
                self._clear_active_command(profile_name)
        active_command.task = self._create_active_task(active_command, runner)
        return True

    async def _accept_direction_input(self, chat_id, principal, text):
        state = self._direction_sessions().get(principal["user_id"])
        if not state or text.startswith("/") or text in self._direction_button_commands():
            return False
        from telegram_bot_ui import ADMIN_BUTTON_MAP, USER_BUTTON_MAP, LEGACY_BUTTON_MAP, BUTTON_PROFILE_PREFIX
        if text in ADMIN_BUTTON_MAP or text in USER_BUTTON_MAP or text in LEGACY_BUTTON_MAP:
            return False
        # Profile buttons are generated per profile and are never a draft answer (dev 19.10:
        # «📁 Профиль: qa» was answered «Отправьте один или несколько номеров»).
        if text.startswith(BUTTON_PROFILE_PREFIX):
            return False
        # The directions screen only waits for a number; any other text belongs elsewhere.
        if state.get("mode") == "switch" and not re.fullmatch(r"[\d\s,;]+", text.strip()):
            return False
        if text.casefold() == "отмена":
            self._direction_sessions().pop(principal["user_id"], None)
            await self._send_text(chat_id, "Изменения не сохранены.")
            return True
        if not self._direction_valid(principal, state):
            self._direction_sessions().pop(principal["user_id"], None)
            await self._send_text(chat_id, "Профиль или направление изменились. Изменения не сохранены; откройте /directions заново.")
            return True
        try:
            mode = state.get("mode")
            if mode == "switch":
                indexes = parse_selection(text, len(state["choices"]))
                if len(indexes) != 1:
                    raise ValueError("Выберите одно направление.")
                state.update(mode="draft", activate=True, draft=state["choices"][indexes[0]])
            elif mode == "catalog":
                indexes = parse_selection(text, len(state["catalog"]))
                items = [state["catalog"][index] for index in indexes]
                state["mode"] = "working"
                await self._direction_job(chat_id, principal, state, "add" if state.get("add") else "new", items)
                return True
            elif mode == "variant":
                before = copy.deepcopy(state["draft"])
                variants = state["draft"]["variants"]
                indexes = parse_selection(text, len(variants))
                operation = state["operation"]
                if operation == "select":
                    if len(indexes) != 1 or variants[indexes[0]].get("status", "active") != "active":
                        raise ValueError("Выберите одно включённое резюме.")
                    first = variants[indexes[0]]
                    state["draft"]["variants"] = [first] + [v for v in variants if v is not first]
                    state["draft"]["selection_strategy"] = "single"
                elif operation == "remove":
                    state["draft"]["variants"] = [v for i, v in enumerate(variants) if i not in indexes]
                else:
                    for index in indexes:
                        variants[index]["status"] = "paused" if operation == "pause" else "active"
                if not any(v.get("status", "active") == "active" for v in state["draft"]["variants"]):
                    # Keep an invalid operation from mutating even the pending base.
                    state["draft"] = before
                    raise ValueError("Оставьте хотя бы одно включённое резюме.")
                state.update(mode="draft", summary=False)
            elif mode == "edit":
                field = state.get("field", "search_queries")
                if field == "search_queries":
                    state["draft"][field] = search_query_suggester.normalize_queries(text)
                elif field in {"work_formats", "fly_allowed", "work_cities"}:
                    conditions = copy.deepcopy(state["draft"].get("work_conditions", work_conditions.defaults(state["draft"])))
                    value = text.strip().casefold().replace("ё", "е")
                    if field == "work_cities":
                        conditions["allowed_cities"] = [] if value == "любой" else [v.strip() for v in text.split(",")]
                    elif field == "fly_allowed":
                        if value not in {"да", "нет"}:
                            raise ValueError("Разрешена ли вахта? Отправьте «да» или «нет».")
                        conditions["allow_fly_in_fly_out"] = value == "да"
                    else:
                        labels = {"удаленно": "remote", "гибрид": "hybrid", "на месте": "on_site",
                                  "на месте работодателя": "on_site", "офис": "on_site", "разъездная": "field"}
                        values = [v.strip() for v in value.split(",")]
                        if value == "любой":
                            conditions["allowed_formats"] = list(work_conditions.FORMAT_LABELS)
                        elif not values or any(v not in labels for v in values):
                            raise ValueError("Укажите через запятую: удалённо, гибрид, на месте, разъездная. Или «любой».")
                        else:
                            conditions["allowed_formats"] = list(dict.fromkeys(labels[v] for v in values))
                    state["draft"]["work_conditions"] = work_conditions.validate(conditions)
                elif field in {"relevant_keywords", "exclude_keywords"}:
                    values = list(dict.fromkeys(v.strip() for v in re.split(r"[\n,]+", text) if v.strip()))
                    if len(values) > 30 or any(len(value) > 100 for value in values):
                        raise ValueError("Не больше 30 слов/фраз, до 100 символов каждое.")
                    state["draft"][field] = values
                elif field in {"goal"}:
                    state["draft"][field] = [v.strip() for v in text.splitlines() if v.strip()]
                elif field == "positioning":
                    if len(text.strip()) > 1500:
                        raise ValueError("Описание: до 1500 символов.")
                    state["draft"][field] = text.strip()
                elif field == "cover_letter_fallback":
                    from fallback_letter import validate
                    state["draft"][field] = validate(text)
                elif field == "salary_baseline":
                    if text.strip() == "0":
                        state["draft"].pop(field, None)
                    elif not text.strip().isdigit() or int(text.strip()) <= 0:
                        raise ValueError("Отправьте положительную сумму в рублях; 0 — убрать ориентир.")
                    else:
                        state["draft"][field] = int(text.strip())
                elif field == "search_profiles":
                    rows = []
                    for line in text.splitlines():
                        parts = line.strip().split()
                        if not parts:
                            continue
                        if len(parts) > 2 or not parts[0].isdigit() or int(parts[0]) <= 0:
                            raise ValueError("Каждая строка: ID города и формат, например: 2 или 113 flyInFlyOut.")
                        schedule = parts[1] if len(parts) == 2 else ""
                        if schedule not in {"", "remote", "fullDay", "flexible", "shift", "flyInFlyOut"}:
                            raise ValueError("Неизвестный формат работы.")
                        row = {"area": parts[0], "schedule": schedule}
                        if row not in rows:
                            rows.append(row)
                    if not rows or len(rows) > 30:
                        raise ValueError("Отправьте от 1 до 30 строк города и формата.")
                    state["draft"][field] = rows
                elif field in {"city", "format"}:
                    profiles = state["draft"].get("search_profiles") or [{"area": "1", "schedule": ""}]
                    if field == "city":
                        city_aliases = {"москва": "1", "санкт-петербург": "2", "спб": "2", "россия": "113"}
                        areas = [city_aliases.get(v.strip().lower(), v.strip()) for v in text.split(",") if v.strip()]
                        if not areas or any(not area.isdigit() or int(area) <= 0 for area in areas):
                            raise ValueError("Отправьте ID городов HH через запятую (Москва — 1, Санкт-Петербург — 2, Россия — 113).")
                        profiles = [{"area": area, "schedule": item["schedule"]} for area in dict.fromkeys(areas)
                                    for item in {v["schedule"]: v for v in profiles}.values()]
                    else:
                        aliases = {"удалённо": "remote", "удаленно": "remote", "офис": "fullDay", "гибкий": "flexible", "вахта": "flyInFlyOut", "любой": ""}
                        values = [aliases.get(v.strip().lower(), v.strip()) for v in text.split(",")]
                        if not values or any(v not in {"", "remote", "fullDay", "flexible", "shift", "flyInFlyOut"} for v in values):
                            raise ValueError("Форматы через запятую: удалённо, офис, гибкий, любой.")
                        profiles = [{"area": area, "schedule": value} for area in dict.fromkeys(v["area"] for v in profiles)
                                    for value in dict.fromkeys(values)]
                    state["draft"]["search_profiles"] = profiles
                elif field == "title":
                    if not 2 <= len(text.strip()) <= 100:
                        raise ValueError("Название: от 2 до 100 символов.")
                    state["draft"][field] = text.strip()
                elif field == "filter_policy":
                    if text.strip() not in {"qa", "generic"}:
                        raise ValueError("Отправьте qa или generic.")
                    state["draft"][field] = text.strip()
                state["mode"] = "draft"
            else:
                return False
            await self._direction_show_draft(chat_id, principal, state)
        except (ValueError, IndexError) as exc:
            await self._send_text(chat_id, str(str(exc)))
        return True

    @staticmethod
    def _direction_button_commands():
        return {
            "🧭 Направления": "/directions", "➕ Новое направление": "/direction_new",
            "🔀 Переключить направление": "/direction_switch", "🧾 Варианты направления": "/direction_variants",
            "➕ Добавить варианты": "/direction_add", "⏸ Пауза варианта": "/direction_pause",
            "▶️ Включить вариант": "/direction_resume", "➖ Удалить вариант": "/direction_remove",
            "🔄 Тексты направления": "/direction_refresh", "✨ Запросы направления": "/direction_suggest",
            "1️⃣ Один вариант": "/direction_single", "🅰️🅱️ A/B варианты": "/direction_ab",
            "✅ Сохранить направление": "/direction_save", "✏️ Править направление": "/direction_edit",
            "❌ Отменить направление": "/direction_cancel",
        }

    @telegram_access.scoped_request
    @read_command
    async def _read_direction(self, chat_id, principal, command, arg):
        return await self._dispatch_direction(chat_id, principal, command, arg)

    @telegram_access.scoped_request
    async def _dispatch_direction(self, chat_id, principal, command, arg):
        import directions
        if (profile_access.current_scope()[1]
                and command in {"/direction_switch", "/direction_new"}):
            raise PermissionError("Tester не может создавать или переключать направление.")
        profile_name, profile, active = self._direction_context(principal)
        state = self._direction_sessions().get(principal["user_id"])
        if state and not self._direction_valid(principal, state):
            self._direction_sessions().pop(principal["user_id"], None)
            state = None
            if command in {"/direction_save", "/search_use_draft"}:
                await self._send_text(chat_id, "Профиль или направление изменились. Устаревшие изменения не сохранены.")
                return
        if command in {"/directions", "/direction_switch"}:
            choices = directions.list_directions(profile.home_dir)
            state = self._direction_bind(principal, mode="switch", choices=choices)
            rows = [[(("✓ " if active and item["name"] == active["name"] else "") + item["title"] + (" (сейчас)" if active and item["name"] == active["name"] else ""), f"choose:{i}")]
                    for i, item in enumerate(choices)]
            if not profile_access.current_scope()[1]:
                rows += [[("➕ Новое направление", "new")]]
            rows += [[("📊 Статистика резюме", "stats")]]
            await self._send_text(chat_id, "🧭 Направления — выберите кнопкой ниже.",
                                  reply_markup=self._direction_markup())
            await self._send_text(chat_id, (f"Ваши направления · профиль {profile_name}\n" +
                                  (f"Сейчас бот ищет по направлению «{active['title']}»." if active else "Направление для поиска пока не выбрано.") +
                                  "\nНажмите на него, чтобы открыть настройки, или на другое, чтобы переключиться."),
                                  reply_markup=self._direction_inline(state, rows))
        elif command == "/direction_summary":
            if not active:
                await self._dispatch_direction(chat_id, principal, "/directions", "")
                return
            state = self._direction_bind(principal, mode="draft", draft=copy.deepcopy(active), texts={}, summary=True)
            await self._direction_show_draft(chat_id, principal, state)
        elif command == "/direction_cancel":
            self._direction_sessions().pop(principal["user_id"], None)
            await self._send_text(chat_id, "Изменения не сохранены. Настройки направления не менялись.", reply_markup=self._direction_markup())
        elif command in {"/direction_save", "/search_use_draft"}:
            if self._has_active_command(profile_name):
                await self._send_busy_status(chat_id, principal, profile_name=profile_name)
                return False
            if not state or state.get("mode") != "draft":
                await self._send_text(chat_id, "Нет несохранённых изменений. Откройте «Направления».")
                return False
            committed = False
            try:
                if state.get("activate"):
                    # Reject a target edited since the switch prompt as well.
                    if directions.load_direction(profile.home_dir, state["draft"]["name"]) != state["draft"]:
                        raise ValueError("Выбранное направление изменилось; выберите его заново.")
                    directions.set_active_direction(profile.home_dir, state["draft"]["name"])
                else:
                    saved = directions.save_direction(profile.home_dir, state["draft"], texts=state.get("texts") or {})
                    if not active:
                        directions.set_active_direction(profile.home_dir, saved["name"])
                committed = True
                self._direction_sessions().pop(principal["user_id"], None)
                note = ""
                try:
                    if self._reload_profile_daemon(profile_name):
                        note = " Повтор перезапущен."
                except RuntimeError:
                    note = " Повтор не удалось перезапустить; проверьте статус."
                await self._send_text(chat_id, "Направление сохранено." + note, reply_markup=self._direction_markup())
            except (ValueError, OSError) as exc:
                await self._send_text(chat_id, str(str(exc)), reply_markup=self._direction_markup(draft=True))
                return False if isinstance(exc, ValueError) and not committed else None
        elif command in {"/direction_new", "/direction_add"}:
            return await self._direction_catalog(chat_id, principal, add=command == "/direction_add")
        elif command in {"/direction_variants", "/search_resume", "/direction_pause", "/direction_resume", "/direction_remove"}:
            operation = {"/direction_pause": "pause", "/direction_resume": "resume", "/direction_remove": "remove"}.get(command, "select")
            await self._direction_prompt_variants(chat_id, principal, operation)
        elif command.startswith("/search_application_"):
            await self._request_application_mode(chat_id, principal, command.removeprefix("/search_application_"))
        elif command in {"/direction_single", "/direction_ab"}:
            if not active:
                await self._ask_choose_direction(chat_id, principal, "Сначала выберите направление:")
                return
            state = self._direction_bind(principal, mode="draft", draft=copy.deepcopy(active), texts={})
            state["draft"]["selection_strategy"] = "ab" if command == "/direction_ab" else "single"
            await self._direction_show_draft(chat_id, principal, state)
        elif command in {"/direction_edit", "/search_edit"}:
            if not state or not state.get("draft") or state.get("activate"):
                if not active:
                    await self._send_text(chat_id, "Сначала создайте новое направление: /direction_new.")
                    return
                state = self._direction_bind(principal, draft=copy.deepcopy(active), texts={})
            if not arg.strip() and command == "/direction_edit":
                await self._direction_edit_menu(chat_id, state)
                return
            state["summary"] = False
            field = arg.strip() or "search_queries"
            aliases = {"queries": "search_queries", "filter": "filter_policy", "relevant": "relevant_keywords", "exclude": "exclude_keywords", "salary": "salary_baseline"}
            field = aliases.get(field, field)
            if field not in {"title", "search_queries", "relevant_keywords", "exclude_keywords", "filter_policy", "positioning", "goal", "city", "format", "search_profiles", "salary_baseline", "work_formats", "fly_allowed", "work_cities", "cover_letter_fallback"}:
                await self._send_text(chat_id, "Поля: /direction_edit title|queries|relevant|exclude|filter")
                return
            state.update(mode="edit", field=field)
            if field == "cover_letter_fallback":
                from fallback_letter import legacy_offer
                rows = []
                if state["draft"].get(field):
                    rows.append([("Убрать письмо", "letterclear")])
                if not state["draft"].get(field) and legacy_offer(profile.home_dir):
                    rows.append([("Посмотреть прежнее письмо", "letteroffer")])
                rows.append([("Назад", "back"), ("Отмена", "cancel")])
                await self._send_text(chat_id,
                    f"Запасное сопроводительное письмо · {state['draft']['title']}\n\n" +
                    (state["draft"].get(field) or "Пока не задано.") +
                    "\n\nОтправьте новый текст — до 1500 символов. Он используется, если не удалось подготовить письмо для вакансии. "
                    "Изменения вступят в силу после сохранения направления.",
                    reply_markup=self._direction_inline(state, rows))
                return
            if field == "work_cities":
                await self._send_text(chat_id,
                    "В каких городах допустима работа с выездом к работодателю? Укажите полные названия через запятую, например: Санкт-Петербург.\n"
                    "«Любой» — убрать ограничение. На полностью удалённую работу это ограничение не влияет.",
                    reply_markup=self._direction_inline(state, [[("Назад", "back"), ("Отмена", "cancel")]]))
                return
            if field in {"work_formats", "fly_allowed"}:
                message = ("Какая работа допустима? Укажите через запятую: удалённо, гибрид, на месте, разъездная. Или «любой»."
                           if field == "work_formats" else "Разрешена ли вахта в этом направлении? Отправьте «да» или «нет».")
                choices = ([("Удалённо и гибрид", "remote"), ("Любой формат", "any")] if field == "work_formats" else
                           [("Да", "yes"), ("Нет", "no")])
                rows = [[(label, "conditionpreset:" + value)] for label, value in choices]
                rows.append([("Назад", "back"), ("Отмена", "cancel")])
                await self._send_text(chat_id, message, reply_markup=self._direction_inline(state, rows))
                return
            if field in {"city", "format", "search_profiles"}:
                choices = ({"Москва": "moscow", "Санкт-Петербург": "spb", "Россия": "russia"} if field == "city" else
                           {"Удалённо": "remote", "Офис": "office", "Вахта": "fly", "Любой": "any"})
                rows = [[(label, "preset:" + value)] for label, value in choices.items()] if field != "search_profiles" else []
                rows += [[("Добавить СПб · любой", "pair:spb")], [("Добавить РФ · вахта", "pair:fly")], [("Назад", "back"), ("Отмена", "cancel")]]
                await self._send_text(chat_id, "Выберите значение или отправьте текст. Город: ID HH через запятую. Формат: remote, fullDay, flyInFlyOut.\n"
                    "Для независимых сочетаний выберите «Города и форматы»: каждая строка содержит ID города и формат, например:\n2\n113 flyInFlyOut", reply_markup=self._direction_inline(state, rows))
                return
            label = {"title": "названия", "search_queries": "запросов", "positioning": "описания", "goal": "должностей",
                     "salary_baseline": "зарплаты от", "relevant_keywords": "подходящих слов", "exclude_keywords": "исключений", "filter_policy": "фильтра"}[field]
            await self._send_text(chat_id, f"Отправьте новое значение {label}. Поисковые слова и должности — по одному в строке; ключевые слова — через запятую.\n"
                                  "Зарплата — сумма в рублях, 0 убирает ориентир. Назад — вернуться к несохранённым изменениям.",
                                  reply_markup=self._direction_inline(state, [[("Назад", "back"), ("Отмена", "cancel")]]))
        elif command in {"/direction_suggest", "/search_suggest", "/direction_refresh"}:
            if not active:
                await self._send_text(chat_id, "Сначала создайте новое направление: /direction_new.")
                return
            state = self._direction_bind(principal, mode="working", draft=copy.deepcopy(active), texts={})
            await self._direction_job(chat_id, principal, state, "refresh" if command == "/direction_refresh" else "suggest", active["variants"])
        elif command == "/direction_stats_refresh":
            if self._has_active_command(profile_name):
                await self._send_busy_status(chat_id, principal, profile_name=profile_name)
                return False
            job = self._mark_active_command(principal=principal, label="negotiation snapshot", profile_name=profile_name)
            await self._send_text(chat_id, "🔄 Загружаю отклики с hh.ru. Это займёт пару минут, работодателям ничего не отправится…")

            async def runner():
                from hh import negotiation_snapshot
                try:
                    results = await asyncio.to_thread(negotiation_snapshot.refresh_for_chats,
                                                      profile.home_dir, profile.hh.cookies_file)
                    done = all(item.get("complete") for item in results)
                    throttled = all(item.get("complete") or item.get("stop_reason") == "refresh_throttled"
                                    for item in results)
                    note = ("✅ Данные обновлены." if done else
                            "Данные недавно запрашивались. Покажу сохранённую статистику." if throttled else
                            "⚠️ Собрал не всё (" + ", ".join(str(item.get("stop_reason")) for item in results
                                                         if not item.get("complete")) + ") — покажу, что есть.")
                    await self._send_text(chat_id, note)
                finally:
                    self._clear_active_command(profile_name)
                await self._dispatch_direction(chat_id, principal, "/direction_stats", "")
            job.task = self._create_active_task(job, runner)
        elif command in {"/direction_stats", "/direction_csv"}:
            if command == "/direction_csv" and principal.get("role") != "admin":
                await self._send_text(chat_id, "Нужны права администратора.")
                return
            import direction_stats
            summary = await read_thread(direction_stats.summarize, profile.home_dir)
            from telegram_app import history
            scope = history.scope_arg(arg)
            summary = direction_stats.scoped(summary, profile.home_dir, scope)
            if command == "/direction_csv":
                await self._send_document(chat_id, filename=f"{profile_name}_directions.csv", content=direction_stats.export_csv(summary).encode("utf-8-sig"))
            else:
                rows = summary.get("rows", [])
                owner = f" · профиль {profile_name}" if principal.get("role") == "admin" else ""
                invited_total = sum(int(row.get("next_stage", 0) or 0) for row in rows) + sum(int(summary.get(bucket, {}).get("next_stage", 0) or 0) for bucket in ("unmatched", "ambiguous"))
                heading = ("Все направления" if scope == "*" else "Отклики без направления" if scope == "?" else f"«{summary['direction_title']}»")
                lines = [f"📊 {heading}: {counted(summary.get('total_topics'), ('отклик', 'отклика', 'откликов'))}, {counted(invited_total, ('приглашение', 'приглашения', 'приглашений'))}{owner}", ""]
                previous_direction = None
                for row in rows[:12]:
                    title = str(row.get("direction_title") or "Направление без названия")
                    if scope == "*" and title != previous_direction:
                        lines += [title]
                        previous_direction = title
                    variant = str(row.get("variant_title") or "Резюме без названия")
                    applied = int(row.get("applied", 0) or 0)
                    invited = int(row.get("next_stage", 0) or 0)
                    share = f" ({invited / applied * 100:.1f}%)".replace(".", ",") if applied else ""
                    lines += [f"«{variant}»",
                              f"{counted(applied, ('отклик', 'отклика', 'откликов'))} · просмотрели {row.get('viewed', 0)} · "
                              f"отказов {row.get('rejected', 0)} · приглашений {invited}{share} · "
                              f"ждут {row.get('no_response', 0)}", ""]
                other = summary.get('unmatched', {}).get('topics', 0)
                if other:
                    lines.append(f"Отклики без направления: {other}")
                ambiguous = summary.get("ambiguous", {}).get("topics", 0)
                if ambiguous:
                    lines.append(f"Отклики без направления (резюме относится к нескольким): {ambiguous}")
                mismatches = await read_thread(direction_stats.resume_mismatches, profile.home_dir)
                view = history.HistoryScope(profile.home_dir, scope)
                mismatches = [m for m in mismatches if view.matches({"requested_resume_id": m.get("requested_resume_hash")})]
                if mismatches:
                    lines += ["", f"⚠️ Ушли НЕ тем резюме: {len(mismatches)}"]
                    for item in mismatches[:5]:
                        lines.append(f"• {item['title'][:60]} ({item['company'][:40]}): ушло «{item['actual_title'] or 'резюме без названия'}» "
                                     f"вместо «{item['requested_title'] or 'резюме без названия'}»")
                lines.append("")
                lines.append("Приглашение — работодатель позвал дальше: на собеседование, тестовое или предложил работу.")
                lines.append("Данные с hh.ru.")
                if principal.get("role") == "admin":
                    lines.append("Скачайте полный список: /direction_csv; /direction_csv all — все направления")
                if not summary.get("total_topics"):
                    lines = [f"📊 {heading}: данных пока нет.",
                             "Нажмите «Обновить с hh.ru»: бот загрузит ваши отклики. Это займёт пару минут, работодателям ничего не отправится.",
                             "Дальше данные обновляются сами после каждого поиска."]
                state = state or self._direction_bind(principal, mode="switch", choices=[])
                buttons = self._direction_inline(state, [[("🔄 Обновить с hh.ru", "statsrefresh")], [("Назад к направлениям", "list")]])
                buttons["inline_keyboard"] += history.markup(profile.home_dir, "/direction_stats", scope=scope)["inline_keyboard"]
                await self._send_text(chat_id, str("\n".join(lines)), reply_markup=buttons)
