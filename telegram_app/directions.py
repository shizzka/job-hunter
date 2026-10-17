"""Profile-bound, confirmed Telegram direction drafts.

Raw HH exports stay in memory until Save; every action rechecks the selected
profile and the exact base direction, including async completion.
"""
from __future__ import annotations

import copy
from contextlib import asynccontextmanager
import hashlib
import html
import json
from pathlib import Path
import re
import uuid

import search_query_suggester


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


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
                 "base": _fingerprint(active), **fields}
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
                [["➕ Новое направление", "🔀 Переключить направление"],
                 ["🧾 Варианты направления", "➕ Добавить варианты"],
                 ["⏸ Пауза варианта", "▶️ Включить вариант", "➖ Удалить вариант"],
                 ["🔄 Тексты направления", "✨ Запросы направления"],
                 ["1️⃣ Один вариант", "🅰️🅱️ A/B варианты"], ["✏️ Править направление"]])
        rows.append(["🎯 Направления", "🏠 Главное"])
        return {"keyboard": [[{"text": label} for label in row] for row in rows], "resize_keyboard": True}

    async def _direction_show_draft(self, chat_id, principal, state):
        draft = state.get("draft") or {}
        if state.get("activate"):
            text = f"Переключить активное направление на «{draft.get('title', '')}»?"
        else:
            variants = "\n".join(f"{i}. {v.get('title') or v['hh_resume_hash']} · {v.get('status', 'active')}"
                                   for i, v in enumerate(draft.get("variants", []), 1))
            text = (f"Черновик: {draft.get('title', '')}\nФильтр: {draft.get('filter_policy', '')} · "
                    f"выбор: {draft.get('selection_strategy', 'single')}\n"
                    f"Режим откликов: {draft.get('application_mode', 'из профиля')}\n\n"
                    "Запросы:\n" + "\n".join(draft.get("search_queries", [])) +
                    "\n\nПодходящие слова: " + ", ".join(draft.get("relevant_keywords", [])) +
                    "\nИсключения: " + ", ".join(draft.get("exclude_keywords", [])) + "\n\n" + variants)
        await self._send_text(chat_id, html.escape(text) + "\n\nИзменения вступят в силу только после сохранения.",
                              reply_markup=self._direction_markup(draft=True))

    async def _direction_prompt_variants(self, chat_id, principal, operation):
        _, _, active = self._direction_context(principal)
        if not active:
            await self._send_text(chat_id, "Сначала создайте или выберите направление: /directions.")
            return
        state = self._direction_bind(principal, mode="variant", operation=operation, draft=copy.deepcopy(active), texts={})
        lines = ["Варианты активного направления. Отправьте номер" + ("а через пробел:" if operation != "select" else ":")]
        for index, variant in enumerate(active["variants"], 1):
            number = variant.get("hh_resume_numeric_id")
            lines.append(f"{index}. {variant.get('title') or variant['hh_resume_hash']} · {variant.get('status', 'active')} · ID: {number if number is not None else 'неизвестен'}")
        if operation == "select":
            lines.append("Выбранный вариант станет единственным для откликов (режим single). Подтвердите черновик.")
        lines.append("Отмена: /direction_cancel")
        await self._send_text(chat_id, html.escape("\n".join(lines)), reply_markup=self._direction_markup())

    async def _direction_catalog(self, chat_id, principal, *, add=False):
        profile_name, profile, active = self._direction_context(principal)
        if add and not active:
            await self._send_text(chat_id, "Сначала выберите направление: /directions.")
            return
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return
        state = self._direction_bind(principal, mode="working", add=add,
                                     draft=copy.deepcopy(active) if add else None)
        command = self._mark_active_command(principal=principal, label="direction catalog", profile_name=profile_name)
        await self._send_text(chat_id, "Читаю полный каталог резюме из вашей HH-сессии…")

        async def runner():
            try:
                # The legacy QA catalog can omit other professions. Read the
                # authenticated catalog without title filters or changing HH.
                if not self._direction_current(principal, state):
                    raise ValueError("Профиль, направление или черновик изменились; каталог отменён.")
                catalog = await self._direction_load_catalog(profile, chat_id=chat_id)
                if not self._direction_current(principal, state):
                    raise ValueError("Профиль, направление или черновик изменились; каталог отменён.")
                if not catalog:
                    raise ValueError("Нет резюме в HH-сессии. Проверьте вход HH: /hh_auth.")
                state.update(mode="catalog", catalog=copy.deepcopy(catalog))
                lines = ["Выберите одно или несколько резюме: отправьте номера через пробел (например, 1 3)."]
                lines.extend(f"{i}. {item.get('title') or item.get('id')}" for i, item in enumerate(catalog, 1))
                lines.append("Тексты будут скачаны из вашей HH-сессии. Отмена: /direction_cancel")
                await self._send_text(chat_id, html.escape("\n".join(lines)), reply_markup=self._direction_markup())
                command.result = {"ok": True}
            except Exception as exc:
                command.result = {"ok": False}
                await self._send_text(chat_id, html.escape(f"Не удалось прочитать каталог: {exc}"))
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
                    suggestion = await search_query_suggester.suggest_direction(resumes=resumes, current_direction=state.get("draft"))
                    draft.update(suggestion)
                if not self._direction_current(principal, state):
                    raise ValueError("Профиль, направление или черновик изменились; результат отменён.")
                state.update(mode="draft", draft=draft, texts=texts)
                await self._direction_show_draft(chat_id, principal, state)
                active_command.result = {"ok": True}
            except Exception as exc:
                active_command.result = {"ok": False}
                await self._send_text(chat_id, html.escape(f"Не удалось подготовить направление: {exc}"))
            finally:
                self._clear_active_command(profile_name)
        active_command.task = self._create_active_task(active_command, runner)

    async def _accept_direction_input(self, chat_id, principal, text):
        state = self._direction_sessions().get(principal["user_id"])
        if not state or text.startswith("/") or text in self._direction_button_commands():
            return False
        from telegram_bot_ui import ADMIN_BUTTON_MAP, USER_BUTTON_MAP, LEGACY_BUTTON_MAP
        if text in ADMIN_BUTTON_MAP or text in USER_BUTTON_MAP or text in LEGACY_BUTTON_MAP:
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
                    state["draft"] = copy.deepcopy(self._direction_context(principal)[2])
                    raise ValueError("Оставьте хотя бы один активный вариант.")
                state["mode"] = "draft"
            elif mode == "edit":
                field = state.get("field", "search_queries")
                if field == "search_queries":
                    state["draft"][field] = search_query_suggester.normalize_queries(text)
                elif field in {"relevant_keywords", "exclude_keywords"}:
                    values = list(dict.fromkeys(v.strip() for v in re.split(r"[\n,]+", text) if v.strip()))
                    if len(values) > 30 or any(len(value) > 100 for value in values):
                        raise ValueError("Не больше 30 слов/фраз, до 100 символов каждое.")
                    state["draft"][field] = values
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
            await self._send_text(chat_id, html.escape(str(exc)))
        return True

    @staticmethod
    def _direction_button_commands():
        return {
            "🎯 Направления": "/directions", "➕ Новое направление": "/direction_new",
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
            lines = [f"Направления · {profile_name}", "Активное: " + (active.get("title", "") if active else "нет; используются прежние настройки")]
            lines.extend(f"{i}. {'✅ ' if active and item['name'] == active['name'] else ''}{item['title']}" for i, item in enumerate(choices, 1))
            if choices:
                self._direction_bind(principal, mode="switch", choices=choices)
                lines.append("Для переключения отправьте номер и сохраните черновик.")
            lines.append("Создание: /direction_new. Правка: /direction_edit. Варианты: /direction_variants.")
            if principal.get("role") == "admin":
                lines.append("Сводка: /direction_stats · CSV: /direction_csv")
            await self._send_text(chat_id, html.escape("\n".join(lines)), reply_markup=self._direction_markup())
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
                await self._send_text(chat_id, html.escape(str(exc)), reply_markup=self._direction_markup(draft=True))
        elif command in {"/direction_new", "/direction_add"}:
            await self._direction_catalog(chat_id, principal, add=command == "/direction_add")
        elif command in {"/direction_variants", "/search_resume", "/direction_pause", "/direction_resume", "/direction_remove"}:
            operation = {"/direction_pause": "pause", "/direction_resume": "resume", "/direction_remove": "remove"}.get(command, "select")
            await self._direction_prompt_variants(chat_id, principal, operation)
        elif command.startswith("/search_application_"):
            if not active:
                return
            state = self._direction_bind(principal, mode="draft", draft=copy.deepcopy(active), texts={})
            state["draft"]["application_mode"] = command.removeprefix("/search_application_")
            await self._direction_show_draft(chat_id, principal, state)
        elif command in {"/direction_single", "/direction_ab"}:
            if not active:
                await self._send_text(chat_id, "Сначала выберите направление: /directions.")
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
            field = arg.strip() or "search_queries"
            aliases = {"queries": "search_queries", "filter": "filter_policy", "relevant": "relevant_keywords", "exclude": "exclude_keywords"}
            field = aliases.get(field, field)
            if field not in {"title", "search_queries", "relevant_keywords", "exclude_keywords", "filter_policy"}:
                await self._send_text(chat_id, "Поля: /direction_edit title|queries|relevant|exclude|filter")
                return
            state.update(mode="edit", field=field)
            await self._send_text(chat_id, f"Отправьте новое значение {field}. Запросы — по одному в строке; ключевые слова — через запятую.\n"
                                  "Другие поля: /direction_edit title|queries|relevant|exclude|filter\nОтмена: /direction_cancel",
                                  reply_markup=self._direction_markup(draft=True))
        elif command in {"/direction_suggest", "/search_suggest", "/direction_refresh"}:
            if not active:
                await self._send_text(chat_id, "Сначала создайте направление: /direction_new.")
                return
            state = self._direction_bind(principal, mode="working", draft=copy.deepcopy(active), texts={})
            await self._direction_job(chat_id, principal, state, "refresh" if command == "/direction_refresh" else "suggest", active["variants"])
        elif command in {"/direction_stats", "/direction_csv"}:
            if principal.get("role") != "admin":
                await self._send_text(chat_id, "Нужны права администратора.")
                return
            import direction_stats
            summary = direction_stats.summarize(profile.home_dir)
            if command == "/direction_csv":
                await self._send_document(chat_id, filename=f"{profile_name}_directions.csv", content=direction_stats.export_csv(summary).encode("utf-8-sig"))
            else:
                rows = summary.get("rows", [])
                lines = [f"Направления · {profile_name} · тем: {summary.get('total_topics', 0)}"]
                for row in rows[:12]:
                    title = str(row.get("direction_title") or row.get("direction") or "Направление")[:100]
                    variant = str(row.get("variant_title") or "Резюме")[:80]
                    lines.append(f"{title} / {variant}: отправлено {row.get('applied', 0)}, "
                                 f"просмотрено {row.get('viewed', 0)}, отказ {row.get('rejected', 0)}, "
                                 f"следующий этап {row.get('next_stage', 0)}, без ответа {row.get('no_response', 0)}")
                lines += [f"Не сопоставлено тем: {summary.get('unmatched', {}).get('topics', 0)}",
                          f"Неоднозначных тем: {summary.get('ambiguous', {}).get('topics', 0)}", "/direction_csv — полная выгрузка"]
                await self._send_text(chat_id, html.escape("\n".join(lines)))
