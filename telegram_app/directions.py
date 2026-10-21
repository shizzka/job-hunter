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


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


STRATEGY_BUTTONS = {
    "single": "1️⃣ Всегда первое резюме",
    "closest": "🎯 Самое подходящее под вакансию",
    "ab": "🅰️🅱️ По очереди (A/B-сравнение)",
}
STRATEGY_HELP = (
    "Каким резюме откликаться в этом направлении?\n\n"
    "1️⃣ Всегда первое — все отклики одним резюме (первым в списке).\n"
    "🎯 Самое подходящее — бот сравнивает название и текст вакансии с каждым резюме и берёт ближайшее.\n"
    "🅰️🅱️ По очереди — резюме чередуются, чтобы через пару недель сравнить, какое работает лучше.\n\n"
    "Текущий выбор отмечен ✅."
)
STRATEGY_LABELS = {
    "single": "одно резюме (первое в списке)",
    "closest": "самое подходящее под вакансию",
    "ab": "A/B — по очереди, чтобы сравнить",
}


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

    def _direction_markup(self, *, draft=False):
        rows = ([["✅ Сохранить направление", "✏️ Править направление"], ["❌ Отменить направление"]] if draft else
                # Actions live on inline buttons of the cards; the reply keyboard stays minimal.
                [])
        rows.append(["🧭 Что ищу", "🏠 Главное"])
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
        return {"inline_keyboard": [[{"text": label, "callback_data": f"dir:{state['token']}:{action}"}
                                      for label, action in row] for row in rows]}

    async def _direction_show_catalog(self, chat_id, state):
        selected = state.setdefault("selected", [])
        rows = [[(("✅ " if i in selected else "☐ ") + str(item.get("title") or item.get("id")))[:90], f"toggle:{i}"]
                for i, item in enumerate(state["catalog"])]
        markup = self._direction_inline(state, [[tuple(row)] for row in rows] +
                                        [[("Далее →", "next")], [("Отмена", "cancel")]])
        await self._send_text(chat_id, "Выберите резюме кнопками, затем нажмите «Далее». Тексты будут скачаны из вашей HH-сессии.", reply_markup=markup)

    async def _direction_show_draft(self, chat_id, principal, state):
        draft = state.get("draft") or {}
        if state.get("activate"):
            text = f"Переключить активное направление на «{draft.get('title', '')}»?"
            rows = [[("Да, переключить", "save")], [("Назад", "list"), ("Отмена", "cancel")]]
        else:
            variants = "\n".join(f"• {v.get('title') or v['hh_resume_hash']} · " +
                                     ("включено" if v.get('status', 'active') == 'active' else "пауза")
                                     for v in draft.get("variants", []))
            profiles = draft.get("search_profiles", [])
            cities = {"1": "Москва", "2": "Санкт-Петербург", "113": "Россия"}
            formats = {"": "любой формат", "remote": "удалённо", "fullDay": "офис", "flyInFlyOut": "вахта", "shift": "сменный", "flexible": "гибкий"}
            profile_labels = [f"{cities.get(str(x['area']), str(x['area']))} / {formats.get(x['schedule'], x['schedule'])}" for x in profiles]
            conditions = draft.get("work_conditions", work_conditions.defaults(draft))
            allowed = ", ".join(work_conditions.FORMAT_LABELS[f] for f in conditions["allowed_formats"])
            text = (f"{'Черновик' if not state.get('summary') else 'Мой поиск'}: {draft.get('title', '')}\n"
                    f"Позиционирование: {draft.get('positioning') or 'не задано'}\n"
                    f"Цель: {', '.join(draft.get('goal', [])) or 'не задана'}\n"
                    "Запросы:\n" + "\n".join(draft.get("search_queries", [])) +
                    "\nГород / формат: " + (", ".join(profile_labels) or "из профиля") +
                    f"\nДопустимая работа: {allowed}. Вахта: {'разрешена' if conditions['allow_fly_in_fly_out'] else 'не разрешена'}." +
                    f"\nГорода места работы: {', '.join(conditions.get('allowed_cities', [])) or 'без ограничений'}." +
                    f"\nЗарплата от: {draft.get('salary_baseline') or 'не задана'} ₽\n"
                    f"Выбор резюме: {STRATEGY_LABELS.get(draft.get('selection_strategy'), 'одно резюме')}\n" + variants)
            rows = [[("✏️ Изменить", "editmenu"), ("🧾 Резюме", "variants")],
                    [("🔀 Как выбирать резюме", "strategy"), ("📊 Статистика", "stats")]]
            if not state.get("summary"):
                rows += [[("✅ Сохранить", "save"), ("Отмена", "cancel")]]
                text += "\n\nИзменения вступят в силу только после сохранения."
            rows += [[("Назад к направлениям", "list")]]
            if state.get("summary"):
                rows += [[("Удалить направление", "delete")]]
        await self._send_text(chat_id, str(text), reply_markup=self._direction_inline(state, rows))

    async def _direction_edit_menu(self, chat_id, state):
        labels = [("Название", "title"), ("Позиционирование", "positioning"), ("Цель", "goal"),
                  ("Запросы", "queries"), ("Город", "city"), ("Формат поиска", "format"), ("Города и форматы", "search_profiles"),
                  ("Допустимые форматы работы", "work_formats"), ("Разрешена ли вахта", "fly_allowed"),
                  ("Города места работы", "work_cities"),
                  ("Зарплата", "salary"), ("Подходящие слова", "relevant"), ("Исключения", "exclude")]
        await self._send_text(chat_id, "Что изменить?", reply_markup=self._direction_inline(state,
            [[(label, f"edit:{field}")] for label, field in labels] + [[("Назад", "back"), ("Отмена", "cancel")]]))

    async def _direction_callback(self, chat_id, principal, data):
        parts = str(data).split(":")
        state = self._direction_sessions().get(principal["user_id"])
        if len(parts) < 3 or not state or parts[1] != state.get("token") or not self._direction_current(principal, state):
            await self._send_text(chat_id, "Эта кнопка устарела. Откройте «Что ищу» заново.")
            return
        action = parts[2]
        try:
            if action == "toggle" and state.get("mode") == "catalog":
                i = int(parts[3])
                if not 0 <= i < len(state["catalog"]):
                    raise ValueError("Резюме не найдено.")
                selected = state.setdefault("selected", [])
                selected.remove(i) if i in selected else selected.append(i)
                await self._direction_show_catalog(chat_id, state)
            elif action == "next" and state.get("mode") == "catalog":
                selected = state.get("selected", [])
                if not selected:
                    raise ValueError("Выберите хотя бы одно резюме.")
                state["mode"] = "working"
                await self._direction_job(chat_id, principal, state, "add" if state.get("add") else "new",
                                          [state["catalog"][i] for i in sorted(selected)])
            elif action == "choose" and state.get("mode") == "switch":
                item = state["choices"][int(parts[3])]
                _, _, active = self._direction_context(principal)
                state.update(mode="draft", draft=copy.deepcopy(item), activate=not active or item["name"] != active["name"],
                             summary=bool(active and item["name"] == active["name"]))
                await self._direction_show_draft(chat_id, principal, state)
            elif action == "statsrefresh":
                await self._dispatch_direction(chat_id, principal, "/direction_stats_refresh", "")
            elif action == "editmenu":
                await self._direction_edit_menu(chat_id, state)
            elif action == "edit":
                await self._dispatch_direction(chat_id, principal, "/direction_edit", parts[3])
            elif action == "preset" and state.get("mode") == "edit":
                values = {"moscow": "1", "spb": "2", "russia": "113", "remote": "remote", "office": "fullDay", "fly": "flyInFlyOut", "any": "любой"}
                await self._accept_direction_input(chat_id, principal, values[parts[3]])
            elif action == "conditionpreset" and state.get("mode") == "edit":
                values = {"yes": "да", "no": "нет", "remote": "удалённо, гибрид", "any": "любой"}
                await self._accept_direction_input(chat_id, principal, values[parts[3]])
            elif action == "pair" and state.get("draft"):
                pairs = {"spb": {"area": "2", "schedule": ""}, "fly": {"area": "113", "schedule": "flyInFlyOut"}}
                pair = pairs[parts[3]]
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
                rows = [[(("✅ " if key == current else "") + STRATEGY_BUTTONS[key], f"strategyset:{key}")]
                        for key in ("single", "closest", "ab")]
                rows.append([("Назад", "back")])
                await self._send_text(chat_id, STRATEGY_HELP, reply_markup=self._direction_inline(state, rows))
            elif action == "strategyset" and len(parts) > 3 and parts[3] in STRATEGY_LABELS:
                saved_direction = bool(state.get("summary"))
                state["draft"]["selection_strategy"] = parts[3]
                state.update(mode="draft", summary=False)
                if saved_direction:
                    # One explicit choice on an existing direction: save it right away.
                    await self._send_text(chat_id, "Выбор резюме: " + STRATEGY_LABELS[parts[3]] + ". Сохраняю…")
                    await self._dispatch_direction(chat_id, principal, "/direction_save", "")
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
                    return
                target = state["delete_target"]
                if directions.load_direction(state["home_dir"], target["name"]) != target:
                    raise ValueError("Направление изменилось; откройте список заново.")
                directions.delete_direction(state["home_dir"], target["name"], expected=target)
                self._reload_profile_daemon(state["profile_name"])
                await self._dispatch_direction(chat_id, principal, "/directions", "")
            elif action in {"list", "new", "save", "cancel", "variants", "stats", "add", "pause", "resume", "remove", "refresh"}:
                command = {"list": "/directions", "new": "/direction_new", "save": "/direction_save", "cancel": "/direction_cancel",
                           "variants": "/direction_variants", "stats": "/direction_stats"}.get(action, "/direction_" + action)
                await self._dispatch_direction(chat_id, principal, command, "")
            elif action == "variant" and state.get("mode") == "variant":
                await self._accept_direction_input(chat_id, principal, str(int(parts[3]) + 1))
        except (ValueError, IndexError, KeyError, OSError) as exc:
            await self._send_text(chat_id, str(str(exc)))

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
        lines = ["Варианты активного направления. Отправьте номер" + ("а через пробел:" if operation != "select" else ":")]
        for index, variant in enumerate(active["variants"], 1):
            number = variant.get("hh_resume_numeric_id")
            lines.append(f"{index}. {variant.get('title') or variant['hh_resume_hash']} · {variant.get('status', 'active')} · ID: {number if number is not None else 'неизвестен'}")
        if operation == "select":
            lines.append("Выбранный вариант станет единственным для откликов (режим single). Подтвердите черновик.")
        lines.append("Отмена: /direction_cancel")
        await self._send_text(chat_id, str("\n".join(lines)), reply_markup=self._direction_inline(state, [[(str(v.get("title") or v["hh_resume_hash"]), f"variant:{i}")] for i, v in enumerate(active["variants"])] + [[("Добавить резюме", "add"), ("Пауза", "pause"), ("Включить", "resume")], [("Убрать вариант", "remove"), ("Обновить тексты", "refresh")], [("Назад", "back"), ("Отмена", "cancel")]]))

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
            return
        state = self._direction_bind(principal, mode="working", add=add,
                                     draft=copy.deepcopy(active) if add else None, texts=texts)
        command = self._mark_active_command(principal=principal, label="direction catalog", profile_name=profile_name)
        await self._send_text(chat_id, "Читаю полный каталог резюме из вашей HH-сессии…")

        async def runner():
            try:
                # The legacy QA catalog can omit other professions. Read the
                # authenticated catalog without title filters or changing HH.
                if not self._direction_current(principal, state):
                    raise ValueError("Профиль, направление или черновик изменились; каталог отменён.")
                from hh.session_probe import probe, OK
                checked = await asyncio.to_thread(probe, profile.hh.cookies_file)
                if checked["status"] != OK:
                    raise ValueError(checked["message"] + " Откройте «🔐 Мой hh» → «Вход в hh», затем повторите.")
                catalog = await self._direction_load_catalog(profile, chat_id=chat_id)
                if not self._direction_current(principal, state):
                    raise ValueError("Профиль, направление или черновик изменились; каталог отменён.")
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
            return
        ai = operation in {"new", "suggest"}
        if ai and not await self._authorize_ai_usage(chat_id, principal, profile_name=profile_name,
                                                    action="direction_suggest_ai_used", check_only=True):
            return
        active_command = self._mark_active_command(principal=principal, label="direction draft", profile_name=profile_name)
        await self._send_text(chat_id, "Готовлю черновик направления. Дождитесь результата; изменения потребуют сохранения.")

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
                        raise ValueError("Профиль или активное направление изменились; черновик отменён.")
                    if not await self._authorize_ai_usage(chat_id, principal, profile_name=profile_name,
                                                         action="direction_suggest_ai_used"):
                        return
                    if not self._direction_current(principal, state):
                        raise ValueError("Профиль, направление или черновик изменились; анализ отменён.")
                    resumes = [{"title": v["title"], "raw": texts.get(v["hh_resume_hash"]) or Path(v["text_file"]).read_text(encoding="utf-8")}
                               for v in draft["variants"] if v.get("status", "active") == "active"]
                    suggestion = await search_query_suggester.suggest_direction(resumes=resumes, current_direction=state.get("draft"), profile_dir=str(profile.home_dir))
                    draft.update(suggestion)
                if not self._direction_current(principal, state):
                    raise ValueError("Профиль, направление или черновик изменились; результат отменён.")
                state.update(mode="draft", draft=draft, texts=texts)
                await self._direction_show_draft(chat_id, principal, state)
                active_command.result = {"ok": True}
            except Exception as exc:
                active_command.result = {"ok": False}
                await self._send_text(chat_id, str(f"Не удалось подготовить направление: {exc}"))
            finally:
                self._clear_active_command(profile_name)
        active_command.task = self._create_active_task(active_command, runner)

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
            await self._send_text(chat_id, "Черновик отменён.")
            return True
        if not self._direction_valid(principal, state):
            self._direction_sessions().pop(principal["user_id"], None)
            await self._send_text(chat_id, "Профиль или направление изменились. Черновик отменён; откройте /directions заново.")
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
                        raise ValueError("Выберите один активный вариант.")
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
                    raise ValueError("Оставьте хотя бы один активный вариант.")
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
                        raise ValueError("Позиционирование: до 1500 символов.")
                    state["draft"][field] = text.strip()
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
            "🧭 Что ищу": "/directions", "🎯 Направления": "/directions", "➕ Новое направление": "/direction_new",
            "🔀 Переключить направление": "/direction_switch", "🧾 Варианты направления": "/direction_variants",
            "➕ Добавить варианты": "/direction_add", "⏸ Пауза варианта": "/direction_pause",
            "▶️ Включить вариант": "/direction_resume", "➖ Удалить вариант": "/direction_remove",
            "🔄 Тексты направления": "/direction_refresh", "✨ Запросы направления": "/direction_suggest",
            "1️⃣ Один вариант": "/direction_single", "🅰️🅱️ A/B варианты": "/direction_ab",
            "✅ Сохранить направление": "/direction_save", "✏️ Править направление": "/direction_edit",
            "❌ Отменить направление": "/direction_cancel",
        }

    async def _dispatch_direction(self, chat_id, principal, command, arg):
        import directions
        profile_name, profile, active = self._direction_context(principal)
        state = self._direction_sessions().get(principal["user_id"])
        if state and not self._direction_valid(principal, state):
            self._direction_sessions().pop(principal["user_id"], None)
            state = None
            if command in {"/direction_save", "/search_use_draft"}:
                await self._send_text(chat_id, "Профиль или направление изменились. Устаревший черновик отменён.")
                return
        if command in {"/directions", "/direction_switch"}:
            choices = directions.list_directions(profile.home_dir)
            state = self._direction_bind(principal, mode="switch", choices=choices)
            rows = [[(("✅ " if active and item["name"] == active["name"] else "") + item["title"], f"choose:{i}")]
                    for i, item in enumerate(choices)]
            rows += [[("➕ Новое направление", "new"), ("📊 Статистика", "stats")]]
            await self._send_text(chat_id, "🧭 Что ищу — ваши направления поиска. Выберите кнопкой ниже.",
                                  reply_markup=self._direction_markup())
            await self._send_text(chat_id, str(f"Направления · {profile_name}\nАктивное: " + (active["title"] if active else "нет")),
                                  reply_markup=self._direction_inline(state, rows))
        elif command == "/direction_summary":
            if not active:
                await self._dispatch_direction(chat_id, principal, "/directions", "")
                return
            state = self._direction_bind(principal, mode="draft", draft=copy.deepcopy(active), texts={}, summary=True)
            await self._direction_show_draft(chat_id, principal, state)
        elif command == "/direction_cancel":
            self._direction_sessions().pop(principal["user_id"], None)
            await self._send_text(chat_id, "Черновик отменён. Настройки направления не менялись.", reply_markup=self._direction_markup())
        elif command in {"/direction_save", "/search_use_draft"}:
            if self._has_active_command(profile_name):
                await self._send_busy_status(chat_id, principal, profile_name=profile_name)
                return
            if not state or state.get("mode") != "draft":
                await self._send_text(chat_id, "Нет готового черновика. Откройте /directions.")
                return
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
        elif command in {"/direction_new", "/direction_add"}:
            await self._direction_catalog(chat_id, principal, add=command == "/direction_add")
        elif command in {"/direction_variants", "/search_resume", "/direction_pause", "/direction_resume", "/direction_remove"}:
            operation = {"/direction_pause": "pause", "/direction_resume": "resume", "/direction_remove": "remove"}.get(command, "select")
            await self._direction_prompt_variants(chat_id, principal, operation)
        elif command.startswith("/search_application_"):
            if not active:
                return
            mode = command.removeprefix("/search_application_")
            labels = {"preview": "только показать", "confirm": "подтверждение", "auto": "автоотклик"}
            from telegram_bot_ui import MENU_APPLICATION_MODE
            back = self._menu_reply_markup(principal, menu=MENU_APPLICATION_MODE)
            if self._has_active_command(profile_name):
                await self._send_busy_status(chat_id, principal, profile_name=profile_name)
                return
            # One explicit choice: saved right away, then back to the «Режим откликов» menu.
            draft = copy.deepcopy(active)
            draft["application_mode"] = mode
            try:
                directions.save_direction(profile.home_dir, draft, texts={})
            except (ValueError, OSError) as exc:
                await self._send_text(chat_id, str(f"❌ Не удалось сохранить режим: {exc}"), reply_markup=back)
                return
            self._direction_sessions().pop(principal["user_id"], None)
            try:
                self._reload_profile_daemon(profile_name)
            except RuntimeError:
                pass
            await self._send_text(chat_id, str(
                f"✅ Режим откликов: {labels.get(mode, mode)} — сохранено (направление «{active['title']}»)."),
                reply_markup=back)
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
                    await self._send_text(chat_id, "Сначала создайте направление: /direction_new.")
                    return
                state = self._direction_bind(principal, draft=copy.deepcopy(active), texts={})
            if not arg.strip() and command == "/direction_edit":
                await self._direction_edit_menu(chat_id, state)
                return
            state["summary"] = False
            field = arg.strip() or "search_queries"
            aliases = {"queries": "search_queries", "filter": "filter_policy", "relevant": "relevant_keywords", "exclude": "exclude_keywords", "salary": "salary_baseline"}
            field = aliases.get(field, field)
            if field not in {"title", "search_queries", "relevant_keywords", "exclude_keywords", "filter_policy", "positioning", "goal", "city", "format", "search_profiles", "salary_baseline", "work_formats", "fly_allowed", "work_cities"}:
                await self._send_text(chat_id, "Поля: /direction_edit title|queries|relevant|exclude|filter")
                return
            state.update(mode="edit", field=field)
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
            label = {"title": "названия", "search_queries": "запросов", "positioning": "позиционирования", "goal": "цели",
                     "salary_baseline": "зарплаты от", "relevant_keywords": "подходящих слов", "exclude_keywords": "исключений", "filter_policy": "фильтра"}[field]
            await self._send_text(chat_id, f"Отправьте новое значение {label}. Запросы и цели — по одному в строке; ключевые слова — через запятую.\n"
                                  "Зарплата — сумма в рублях, 0 убирает ориентир. Назад — вернуться к черновику.",
                                  reply_markup=self._direction_inline(state, [[("Назад", "back"), ("Отмена", "cancel")]]))
        elif command in {"/direction_suggest", "/search_suggest", "/direction_refresh"}:
            if not active:
                await self._send_text(chat_id, "Сначала создайте направление: /direction_new.")
                return
            state = self._direction_bind(principal, mode="working", draft=copy.deepcopy(active), texts={})
            await self._direction_job(chat_id, principal, state, "refresh" if command == "/direction_refresh" else "suggest", active["variants"])
        elif command == "/direction_stats_refresh":
            if self._has_active_command(profile_name):
                await self._send_busy_status(chat_id, principal, profile_name=profile_name)
                return
            job = self._mark_active_command(principal=principal, label="negotiation snapshot", profile_name=profile_name)
            await self._send_text(chat_id, "🔄 Собираю данные об откликах с hh (только чтение). Это займёт пару минут…")

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
            summary = direction_stats.summarize(profile.home_dir)
            if command == "/direction_csv":
                await self._send_document(chat_id, filename=f"{profile_name}_directions.csv", content=direction_stats.export_csv(summary).encode("utf-8-sig"))
            else:
                rows = summary.get("rows", [])
                owner = f" · профиль {profile_name}" if principal.get("role") == "admin" else ""
                lines = [f"📊 Как работают ваши резюме{owner} (по данным hh, откликов всего: {summary.get('total_topics', 0)})", ""]
                for row in rows[:12]:
                    title = str(row.get("direction_title") or row.get("direction") or "Направление")[:100]
                    variant = str(row.get("variant_title") or "Резюме")[:80]
                    applied = int(row.get("applied", 0) or 0)
                    invited = int(row.get("next_stage", 0) or 0)
                    share = f" ({invited / applied * 100:.1f}%)".replace(".", ",") if applied else ""
                    lines.append(f"{title} · «{variant}»: откликов {applied}, просмотрели {row.get('viewed', 0)}, "
                                 f"отказов {row.get('rejected', 0)}, приглашений {invited}{share}, "
                                 f"ждут ответа {row.get('no_response', 0)}")
                other = summary.get('unmatched', {}).get('topics', 0)
                if other:
                    lines.append(f"Другими резюме (не из направлений): откликов {other}")
                mismatches = direction_stats.resume_mismatches(profile.home_dir)
                if mismatches:
                    lines += ["", f"⚠️ Ушли НЕ тем резюме: {len(mismatches)}"]
                    for item in mismatches[:5]:
                        lines.append(f"• {item['title'][:60]} ({item['company'][:40]}): ушло «{item['actual_title'] or item['actual_resume_numeric_id']}» "
                                     f"вместо «{item['requested_title'] or item['requested_resume_numeric_id']}»")
                lines.append("")
                lines.append("Приглашение = работодатель позвал дальше (собеседование, тест, оффер).")
                if principal.get("role") == "admin":
                    lines.append("/direction_csv — полная выгрузка")
                if not summary.get("total_topics"):
                    lines = ["📊 Данных об откликах пока нет — их нужно один раз собрать с hh.",
                             "Нажмите «🔄 Обновить данные» (только чтение, пара минут). Дальше бот обновляет их сам после поиска."]
                state = state or self._direction_bind(principal, mode="switch", choices=[])
                await self._send_text(chat_id, str("\n".join(lines)), reply_markup=self._direction_inline(
                    state, [[("🔄 Обновить данные", "statsrefresh")], [("К направлениям", "list")]]))
