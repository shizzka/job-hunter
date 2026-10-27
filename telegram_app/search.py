"""Telegram search handlers extracted without changing outcomes."""
from __future__ import annotations

from datetime import datetime
import asyncio
import logging
import profile as profile_mod

from telegram_app.command_queue import profile_command
from telegram_app.routing import COMMAND_ROUTES
from telegram_bot_ui import (
    MENU_APPLICATION_MODE,
    MENU_COMPANIES,
    MENU_EXPERIENCE,
    MENU_HH,
    MENU_SEARCH,
    MENU_SEARCH_CONDITIONS,
    ROLE_USER,
    _pretty_profile_name,
    _schedule_preset_label,
    reply_menu_for_command,
)
from telegram_bot_ui import _is_menu_button_text
import client_hh_auth
import company_blacklist
import hh_response_counter
import search_query_suggester

log = logging.getLogger("telegram_bot")


class TelegramSearch:
    def _search_settings_state(self, user_id: int) -> dict:
        state = self._load_state()
        return dict(((state.get("user_state") or {}).get(str(user_id)) or {}))

    def _set_search_settings_state(self, user_id: int, **fields: object) -> dict:
        return self._update_user_state("user_state", user_id, fields=fields)

    def _clear_search_settings_state(self, user_id: int, *keys: str) -> None:
        self._update_user_state("user_state", user_id, remove_keys=keys)

    def _search_settings_text(self, profile_name: str, *, user_id: int) -> str:
        profile = self._profile(profile_name)
        client = self._client_record(user_id) or {}
        queries = profile.hh.search_queries
        lines = [
            f"🎯 Мои поиски · профиль {_pretty_profile_name(profile_name)}",
            "",
            "🔑 Ключевые слова:",
        ]
        lines.extend(f"{index}. {query}" for index, query in enumerate(queries, start=1))
        lines.extend([
            "",
            f"📄 Резюме HH: {profile.hh.primary_resume_title or 'ещё не выбрано'}",
            f"🎯 Направление: {(getattr(profile, 'direction', None) or {}).get('title') or client.get('target_role') or 'не указано'}",
            f"📍 Локация: {client.get('target_location') or 'настроена в профиле'}",
            f"🔁 Повтор: {_schedule_preset_label(profile.search_interval_min)}",
            f"🛡 Режим откликов: {self._application_mode_label(profile.hh.application_mode)}",
            "",
            "ИИ только предложит черновик. Запросы сохраняются или меняются только по вашей команде.",
        ])
        return "\n".join(lines)

    @staticmethod
    def _application_mode_label(value: str) -> str:
        return {
            "preview": "только показать",
            "confirm": "Подтверждать отклик",
            "auto": "Автоматически",
        }.get(value, "Автоматически")

    @staticmethod
    def _experience_label(value: str) -> str:
        return {
            "": "любой",
            "noExperience": "без опыта",
            "between1And3": "1–3 года",
            "between3And6": "3–6 лет",
            "moreThan6": "6+ лет",
        }.get(value, "любой")

    def _search_conditions_text(self, profile_name: str) -> str:
        hh = self._profile(profile_name).hh
        salary = f"{hh.search_salary:,} ₽".replace(",", " ") if hh.search_salary else "не задана"
        return "\n".join([
            "⚙️ Условия поиска",
            "",
            f"👤 Опыт: {self._experience_label(hh.search_experience)}",
            f"💰 Зарплата от: {salary}",
            f"💵 Только с указанной зарплатой: {'да' if hh.search_only_with_salary else 'нет'}",
            "",
            "Изменения сохраняются в вашем профиле и подхватываются повтором сразу.",
        ])

    def _application_mode_text(self, profile_name: str) -> str:
        profile = self._profile(profile_name)
        mode = profile.hh.application_mode
        direction = getattr(profile, "direction", None) or {}
        scope = f" (направление «{direction.get('title')}»)" if direction.get("title") else ""
        return "\n".join([
            "🛡 Режим откликов",
            "",
            f"Сейчас: {self._application_mode_label(mode)}{scope}.",
            "Чтобы включить режим «⚡ Автоматически», нужно ваше подтверждение. Остальные режимы сохраняются сразу.",
            "👀 Только показать — пришлёт подходящие вакансии, без отправки.",
            "✋ Подтверждать отклик — бот присылает вакансию и откликается только после вашего подтверждения на карточке.",
            "⚡ Автоматически — бот откликается сам на подходящие вакансии в пределах лимитов.",
        ])

    async def _save_search_setting(
        self,
        chat_id: int,
        principal: dict,
        updates: dict[str, str | int],
        message: str,
        *,
        menu: str,
    ) -> None:
        profile_name = self._selected_profile(principal)
        profile_mod.update_profile_env(profile_name, updates)
        try:
            restarted = self._reload_profile_daemon(profile_name)
            restart_note = "\n🔁 Повтор перезапущен с новыми настройками." if restarted else ""
        except RuntimeError as exc:
            restart_note = f"\n⚠️ Настройка сохранена, но повтор не перезапустился: {exc}"
        self._set_selected_menu(principal["user_id"], menu)
        await self._send_text(
            chat_id,
            f"✅ {message}" + restart_note,
            reply_markup=self._menu_reply_markup(principal, menu=menu),
        )

    async def _start_market_research(self, chat_id: int, principal: dict, text: str) -> None:
        """Read-only hh search statistics for admin posts; runs as the profile's active command."""
        import market_research
        try:
            queries = market_research.parse_queries(text)
        except market_research.MarketResearchError as exc:
            await self._send_text(chat_id, f"❌ {exc}", reply_markup=self._menu_reply_markup(principal))
            return
        profile_name = self._selected_profile(principal)
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return
        active_command = self._mark_active_command(principal=principal, label="market research", profile_name=profile_name)
        await self._send_text(chat_id, "⏳ Собираю: " + ", ".join(queries) + ". Это займёт пару минут.",
                              reply_markup=self._menu_reply_markup(principal))

        async def runner() -> None:
            try:
                cookies_file = self._profile(profile_name).hh.cookies_file

                def work():
                    with hh_response_counter._authenticated_client(cookies_file) as client:
                        return market_research.collect(client, queries)
                snapshot = await asyncio.to_thread(work)
                summary = market_research.summarize(snapshot)
                await self._send_text(chat_id, str(market_research.summary_text(summary)),
                                      reply_markup=self._menu_reply_markup(principal))
                stamp = datetime.now().strftime("%Y%m%d-%H%M")
                await self._send_document(chat_id, filename=f"hh-market-{stamp}.md",
                                          content=market_research.export_markdown(snapshot, summary),
                                          caption="Отчёт для поста: сводка и таблица вакансий с зарплатой.")
                await self._send_document(chat_id, filename=f"hh-market-{stamp}.csv",
                                          content=market_research.export_csv(snapshot),
                                          caption="Все собранные вакансии; strict_match=True — точное совпадение слов.")
            except market_research.MarketResearchError as exc:
                await self._send_text(chat_id, f"❌ {exc}", reply_markup=self._menu_reply_markup(principal))
            except Exception as exc:
                log.warning("market research failed: %s", type(exc).__name__)
                await self._send_text(chat_id, "❌ Не удалось собрать статистику. Подробности в логе бота.",
                                      reply_markup=self._menu_reply_markup(principal))
            finally:
                self._clear_active_command(profile_name)

        active_command.task = self._create_active_task(active_command, runner)

    async def _accept_search_query_input(self, chat_id: int, principal: dict, text: str) -> bool:
        state = self._search_settings_state(principal["user_id"])
        input_kind = state.get("search_input")
        if input_kind == "market":
            import market_research
            try:
                market_research.parse_queries(text)
            except market_research.MarketResearchError as exc:
                # Keep waiting for titles: the next message is a corrected list, not a command.
                await self._send_text(chat_id, f"❌ {exc} Отправьте список ещё раз или «Отмена».",
                                      reply_markup=self._menu_reply_markup(principal))
                return True
            self._clear_search_settings_state(principal["user_id"], "search_input")
            await self._start_market_research(chat_id, principal, text)
            return True
        if input_kind not in {"queries", "resume", "salary", "company_block", "company_unblock"}:
            return False
        if input_kind in {"queries", "resume"}:
            if state.get("search_input_profile", self._selected_profile(principal)) != self._selected_profile(principal):
                self._clear_search_settings_state(principal["user_id"], "search_input", "search_query_draft", "search_input_profile")
                await self._send_text(chat_id, "Профиль изменился. Откройте изменение настроек заново.")
                return True
            import directions
            if directions.active_direction(self._profile(self._selected_profile(principal)).home_dir):
                self._clear_search_settings_state(principal["user_id"], "search_input", "search_query_draft")
                await self._send_text(chat_id, "Активное направление изменилось. Откройте /search_edit или /search_resume заново.")
                return True
        if _is_menu_button_text(text) or text.startswith("/") and text != "/cancel":
            self._clear_search_settings_state(principal["user_id"], "search_input")
            return False
        if text.strip().casefold() in {"отмена", "/cancel"}:
            self._clear_search_settings_state(principal["user_id"], "search_input")
            await self._send_text(
                chat_id,
                "Изменение запросов отменено.",
                reply_markup=self._menu_reply_markup(
                    principal,
                    menu=(MENU_SEARCH_CONDITIONS if input_kind == "salary" else
                          MENU_HH if input_kind == "resume" else
                          MENU_COMPANIES if input_kind in {"company_block", "company_unblock"} else MENU_SEARCH),
                ),
            )
            return True
        if input_kind in {"company_block", "company_unblock"}:
            profile_name = self._selected_profile(principal)
            try:
                company_blacklist.set_blocked(text, input_kind == "company_block", profile_name)
            except ValueError as exc:
                await self._send_text(chat_id, str(exc))
                return True
            self._clear_search_settings_state(principal["user_id"], "search_input")
            await self._send_text(chat_id, "Список компаний обновлён.", reply_markup=self._menu_reply_markup(principal, menu=MENU_COMPANIES))
            return True
        if input_kind == "salary":
            normalized = text.strip().replace(" ", "").replace("₽", "")
            if not normalized.isdigit() or int(normalized) > 10_000_000:
                await self._send_text(
                    chat_id,
                    "❌ Отправьте сумму цифрами от 0 до 10 000 000. Ноль отключит фильтр. Для отмены: Отмена.",
                    reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH_CONDITIONS),
                )
                return True
            salary = int(normalized)
            self._clear_search_settings_state(principal["user_id"], "search_input")
            await self._save_search_setting(
                chat_id,
                principal,
                {"HH_SEARCH_SALARY": salary},
                f"Минимальная зарплата: {salary:,} ₽".replace(",", " "),
                menu=MENU_SEARCH_CONDITIONS,
            )
            return True
        if input_kind == "resume":
            try:
                index = int(text.strip())
                catalog = client_hh_auth.load_hh_resume_catalog(self._selected_profile(principal))
                item = catalog[index - 1]
                resume_id = str(item.get("id") or "").strip()
                title = str(item.get("title") or "").strip()
                if not resume_id:
                    raise ValueError
                env_file = profile_mod.update_profile_env(
                    self._selected_profile(principal),
                    {
                        "HH_PRIMARY_RESUME_ID": resume_id,
                        "HH_PRIMARY_RESUME_TITLE": title,
                    },
                )
                try:
                    restarted = self._reload_profile_daemon(self._selected_profile(principal))
                    restart_note = "\n🔁 Повтор перезапущен с новым резюме." if restarted else ""
                except RuntimeError as exc:
                    restart_note = f"\n⚠️ Резюме сохранено, но повтор не перезапустился: {exc}"
            except (ValueError, IndexError, TypeError):
                await self._send_text(
                    chat_id,
                    "❌ Отправьте номер резюме из списка или «Отмена».",
                    reply_markup=self._menu_reply_markup(principal, menu=MENU_HH),
                )
                return True
            self._clear_search_settings_state(principal["user_id"], "search_input")
            await self._send_text(
                chat_id,
                f"✅ Основное резюме выбрано: {title or resume_id}\nФайл настроек: {env_file}" + restart_note,
                reply_markup=self._menu_reply_markup(principal, menu=MENU_HH),
            )
            return True
        try:
            queries = search_query_suggester.normalize_queries(text)
            env_file = profile_mod.update_profile_env(
                self._selected_profile(principal),
                {"HH_SEARCH_QUERIES": search_query_suggester.queries_to_env_value(queries)},
            )
            try:
                restarted = self._reload_profile_daemon(self._selected_profile(principal))
                restart_note = "\n🔁 Повтор перезапущен с новыми настройками." if restarted else ""
            except RuntimeError as exc:
                restart_note = f"\n⚠️ Запросы сохранены, но повтор не перезапустился: {exc}"
        except (search_query_suggester.SearchQueryValidationError, ValueError) as exc:
            await self._send_text(
                chat_id,
                f"❌ {exc}\n\nОтправьте список ещё раз: один запрос в строке. Для отмены: Отмена.",
                reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH),
            )
            return True
        self._clear_search_settings_state(principal["user_id"], "search_input", "search_query_draft")
        await self._send_text(
            chat_id,
            "✅ Запросы сохранены для вашего профиля. Следующий поиск возьмёт именно этот список.\n"
            f"Файл настроек: {env_file}" + restart_note,
            reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH),
        )
        return True

    @profile_command
    async def _start_search_query_suggestion(self, chat_id: int, principal: dict, *, profile_name: str) -> None:
        import directions
        if directions.active_direction(self._profile(profile_name).home_dir):
            await self._dispatch_direction(chat_id, principal, "/search_suggest", "")
            return
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return
        if not await self._authorize_ai_usage(chat_id, principal, profile_name=profile_name,
                                             action="search_suggest_ai_used", check_only=True):
            return
        reply_menu = reply_menu_for_command(principal.get("role", ROLE_USER), "/search_suggest")
        active_command = self._mark_active_command(
            principal=principal,
            label="suggest search queries",
            profile_name=profile_name,
        )
        await self._send_text(
            chat_id,
            "✨ Подбираю варианты по вашему резюме. Это черновик: без вашего подтверждения настройки не изменятся.",
            reply_markup=self._menu_reply_markup(principal, menu=reply_menu),
        )

        async def runner() -> None:
            try:
                if not await self._authorize_ai_usage(chat_id, principal, profile_name=profile_name,
                                                     action="search_suggest_ai_used"):
                    return
                profile = self._profile(profile_name)
                client = self._client_record(principal["user_id"]) or {}
                queries = await search_query_suggester.suggest_queries(
                    resume_path=profile.resume_file,
                    target_role=str(client.get("target_role") or ""),
                    current_queries=profile.hh.search_queries,
                )
                if self._selected_profile(principal) != profile_name or directions.active_direction(profile.home_dir):
                    await self._send_text(chat_id, "Профиль или направление изменились; предложение отменено.")
                    return
                import uuid
                from telegram_app import history
                draft_token = uuid.uuid4().hex[:12]
                self._set_search_settings_state(principal["user_id"], search_query_draft=queries,
                    search_query_draft_profile=profile_name, search_query_draft_token=draft_token,
                    search_query_draft_binding=history.token(profile.home_dir))
                rendered = "\n".join(f"{index}. {query}" for index, query in enumerate(queries, start=1))
                await self._send_text(
                    chat_id,
                    "✨ ИИ предложил черновик:\n" + rendered +
                    "\n\nЧтобы применить его, нажмите «✅ Сохранить предложенное». "
                    "Или нажмите «✏️ Изменить запросы» и отправьте свой список.",
                    reply_markup={"inline_keyboard": [
                        [{"text": "✅ Сохранить предложенное", "callback_data": f"qd:{draft_token}:save"}],
                        [{"text": "✏️ Изменить запросы", "callback_data": f"qd:{draft_token}:edit"}]]},
                )
            except Exception as exc:
                log.warning("Search query suggestion failed for %s: %s", profile_name, exc)
                await self._send_text(
                    chat_id,
                    "❌ Не удалось получить предложения ИИ. Текущие запросы не менялись.",
                    reply_markup=self._menu_reply_markup(principal, menu=reply_menu),
                )
            finally:
                self._clear_active_command(profile_name)

        active_command.task = self._create_active_task(active_command, runner)

    @staticmethod
    def _find_effective_mode(profile, direction):
        # _profile() already has the active direction overlaid. Other choices
        # must inherit the underlying profile, exactly as overlay_profile does.
        base = getattr(profile, "_direction_base", profile)
        return direction["application_mode"] if "application_mode" in direction else base.hh.application_mode

    def _find_snapshot(self, profile_name):
        import directions
        import hashlib
        import json
        profile = self._profile(profile_name)
        base_profile = getattr(profile, "_direction_base", profile)
        items = directions.list_directions(profile.home_dir)
        active = directions.active_direction(profile.home_dir)
        base = {"home": profile.home_dir, "items": items, "active": active,
                "hh": vars(profile.hh), "base_hh": vars(base_profile.hh),
                "effective_modes": {item["name"]: self._find_effective_mode(profile, item)
                                    for item in items}}
        return hashlib.sha256(json.dumps(base, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _find_launch_current(self, state):
        try:
            return state.get("snapshot") == self._find_snapshot(state["profile"])
        except (ValueError, OSError, KeyError):
            return False

    async def _show_find_launch(self, chat_id, principal, profile_name, *, selected=None):
        import directions
        import secrets
        profile = self._profile(profile_name)
        items = [item for item in directions.list_directions(profile.home_dir)
                 if not item.get("paused")]
        # A tester can only see and launch their assigned search. The direction
        # store and subsequent command admission retain their own access checks.
        if principal.get("role") == "tester":
            items = [item for item in items if item["name"] == principal.get("direction")]
        state = {"token": secrets.token_hex(6), "profile": profile_name,
                 "snapshot": self._find_snapshot(profile_name),
                 "choices": [item["name"] for item in items]}
        prefix = f"fs:pick:{state['token']}:"
        if selected is None and len(items) > 1:
            rows = [[{"text": item["title"], "callback_data": prefix + str(index)}]
                    for index, item in enumerate(items)]
            rows.append([{"text": "Все поиски", "callback_data": prefix + str(len(items))}])
            text = "🔎 Найти вакансии\n\nВыберите поиск или запустите все поиски."
        else:
            selected = selected if selected is not None else state["choices"]
            chosen = [item for item in items if item["name"] in selected]
            if selected and len(chosen) != len(selected):
                await self._send_text(chat_id, "Поиск изменился. Откройте «Найти вакансии» заново.")
                return
            state["selected"] = selected
            state["auto"] = any(self._find_effective_mode(profile, item) == "auto" for item in chosen) if chosen else profile.hh.application_mode == "auto"
            lines = ["🔎 Найти вакансии сейчас", ""]
            if chosen:
                for item in chosen:
                    lines.extend([f"Поиск: {item['title']}",
                                  f"Режим: {self._application_mode_label(self._find_effective_mode(profile, item))}"])
            else:
                lines.extend(["Поиск: " + ", ".join(profile.hh.search_queries),
                              f"Режим: {self._application_mode_label(profile.hh.application_mode)}"])
            text = "\n".join(lines)
            rows = [[{"text": "🔎 Начать поиск", "callback_data": f"fs:go:{state['token']}"}]]
            if state["auto"]:
                rows.append([{"text": "🧪 Только показать результаты", "callback_data": f"fs:dry:{state['token']}"}])
        self._set_search_settings_state(principal["user_id"], find_launch=state)
        await self._send_text(chat_id, text, reply_markup={"inline_keyboard": rows})

    async def _route_find(self, chat_id, principal, command, arg, profile_name, reply_menu):
        await self._show_find_launch(chat_id, principal, profile_name)

    async def _route_market_research(self, chat_id, principal, command, arg, profile_name, reply_menu):
        if arg.strip():
            await self._start_market_research(chat_id, principal, arg)
            return
        self._set_search_settings_state(principal["user_id"], search_input="market")
        await self._send_text(
            chat_id,
            "📈 Рынок вакансий: соберу свежую статистику hh.ru по названиям вакансий.\n\n"
            "Отправьте названия, по одному в строке (до 20). Например:\n"
            "QA lead\nРуководитель отдела тестирования\nМенеджер по подбору персонала\n\n"
            "Ищу по всей России, в названии вакансии. Откликов и изменений не делаю — только чтение. "
            "Для отмены: Отмена.",
            reply_markup=self._menu_reply_markup(principal),
        )
        return

    async def _route_cli(self, chat_id, principal, command, arg, profile_name, reply_menu):
        flag, label, timeout = COMMAND_ROUTES[command].args
        await self._start_cli_command(chat_id, principal, profile_name, flag, label, timeout)
        return

    async def _route_direction(self, chat_id, principal, command, arg, profile_name, reply_menu):
        direction_menu = reply_menu_for_command(principal.get("role", ROLE_USER), command)
        if direction_menu is not None:
            self._set_selected_menu(principal["user_id"], direction_menu)
        await self._dispatch_direction(chat_id, principal, command, arg)
        return

    async def _route_companies(self, chat_id, principal, command, arg, profile_name, reply_menu):
        if command == "/companies":
            self._set_selected_menu(principal["user_id"], MENU_COMPANIES)
        companies = company_blacklist.list_companies(profile_name)
        message = "🚫 Чёрный список компаний\n" + ("\n".join("• " + x for x in companies) or "Пока пуст.")
        if command != "/companies":
            self._set_search_settings_state(principal["user_id"], search_input=command[1:])
            message += "\n\nПришлите точное название одной компании. Добавить её можно заранее, до поиска и первого отклика. Для отмены — Назад."
        await self._send_text(chat_id, message, reply_markup=self._menu_reply_markup(principal))
        return

    async def _route_search_settings(self, chat_id, principal, command, arg, profile_name, reply_menu):
        import directions
        if directions.active_direction(self._profile(profile_name).home_dir):
            # Show the global keyboard before the active direction card.
            self._set_selected_menu(principal["user_id"], MENU_SEARCH)
            await self._send_text(chat_id, "🎯 Мои поиски — условия текущего поиска ниже.",
                                  reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH))
            await self._dispatch_direction(chat_id, principal, "/direction_summary", "")
            return
        self._set_selected_menu(principal["user_id"], MENU_SEARCH)
        await self._send_text(
            chat_id,
            self._search_settings_text(profile_name, user_id=principal["user_id"]),
            reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH),
        )
        return

    async def _route_search_conditions(self, chat_id, principal, command, arg, profile_name, reply_menu):
        self._set_selected_menu(principal["user_id"], MENU_SEARCH_CONDITIONS)
        await self._send_text(
            chat_id,
            self._search_conditions_text(profile_name),
            reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH_CONDITIONS),
        )
        return

    async def _route_search_experience(self, chat_id, principal, command, arg, profile_name, reply_menu):
        self._set_selected_menu(principal["user_id"], MENU_EXPERIENCE)
        await self._send_text(
            chat_id,
            "👤 Выберите требуемый опыт. Этот фильтр hh.ru применится ко всем вашим запросам.",
            reply_markup=self._menu_reply_markup(principal, menu=MENU_EXPERIENCE),
        )
        return

    async def _route_experience(self, chat_id, principal, command, arg, profile_name, reply_menu):
        value, label = COMMAND_ROUTES[command].args
        await self._save_search_setting(
            chat_id,
            principal,
            {"HH_SEARCH_EXPERIENCE": value},
            f"Фильтр по опыту: {label}.",
            menu=reply_menu,
        )
        return

    async def _route_search_salary(self, chat_id, principal, command, arg, profile_name, reply_menu):
        self._set_search_settings_state(principal["user_id"], search_input="salary")
        await self._send_text(
            chat_id,
            "💰 Отправьте минимальную зарплату в рублях. Например: 150000.\n"
            "Ноль отключит этот фильтр. Для отмены: Отмена.",
            reply_markup=self._menu_reply_markup(principal, menu=reply_menu),
        )
        return

    async def _route_search_salary_toggle(self, chat_id, principal, command, arg, profile_name, reply_menu):
        current = self._profile(profile_name).hh.search_only_with_salary
        await self._save_search_setting(
            chat_id,
            principal,
            {"HH_SEARCH_ONLY_WITH_SALARY": 0 if current else 1},
            "Показывать только вакансии с указанной зарплатой."
            if not current else "Вакансии без указанной зарплаты снова включены.",
            menu=reply_menu,
        )
        return

    async def _route_search_remote_toggle(self, chat_id, principal, command, arg, profile_name, reply_menu):
        # Old keyboards lead to the same per-search editor without reviving a second setting.
        return await self._dispatch_direction(chat_id, principal, "/direction_edit", "work_formats")

    async def _route_search_application_mode(self, chat_id, principal, command, arg, profile_name, reply_menu):
        self._set_selected_menu(principal["user_id"], MENU_APPLICATION_MODE)
        await self._send_text(
            chat_id,
            self._application_mode_text(profile_name),
            reply_markup=self._menu_reply_markup(principal, menu=MENU_APPLICATION_MODE),
        )
        return

    async def _route_application_mode(self, chat_id, principal, command, arg, profile_name, reply_menu):
        await self._request_application_mode(chat_id, principal, COMMAND_ROUTES[command].args[0])
        return

    async def _route_search_edit(self, chat_id, principal, command, arg, profile_name, reply_menu):
        self._set_search_settings_state(principal["user_id"], search_input="queries", search_input_profile=profile_name)
        await self._send_text(
            chat_id,
            "✏️ Отправьте новый список: один поисковый запрос в строке.\n"
            "Например:\nQA engineer\nManual QA Engineer\nТестировщик ПО\n\n"
            "Список заменит текущий. Для отмены отправьте «Отмена».",
            reply_markup=self._menu_reply_markup(principal, menu=reply_menu),
        )
        return

    async def _route_search_suggest(self, chat_id, principal, command, arg, profile_name, reply_menu):
        await self._start_search_query_suggestion(chat_id, principal, profile_name=profile_name)
        return

    async def _route_search_use_draft(self, chat_id, principal, command, arg, profile_name, reply_menu):
        query_state = self._search_settings_state(principal["user_id"])
        if query_state.get("search_query_draft_profile", profile_name) != profile_name:
            self._clear_search_settings_state(principal["user_id"], "search_query_draft", "search_query_draft_profile")
            await self._send_text(chat_id, "Черновик создан для другого профиля. Запросите предложение заново.")
            return
        draft = query_state.get("search_query_draft") or []
        try:
            queries = search_query_suggester.normalize_queries(draft)
            env_file = profile_mod.update_profile_env(
                profile_name,
                {"HH_SEARCH_QUERIES": search_query_suggester.queries_to_env_value(queries)},
            )
            try:
                restarted = self._reload_profile_daemon(profile_name)
                restart_note = "\n🔁 Повтор перезапущен с новыми настройками." if restarted else ""
            except RuntimeError as exc:
                restart_note = f"\n⚠️ Запросы сохранены, но повтор не перезапустился: {exc}"
        except (search_query_suggester.SearchQueryValidationError, ValueError):
            await self._send_text(
                chat_id,
                "ℹ️ Сначала нажмите «✨ Предложить ИИ» или отправьте свой список через «✏️ Изменить запросы».",
                reply_markup=self._menu_reply_markup(principal, menu=reply_menu),
            )
            return
        self._clear_search_settings_state(principal["user_id"], "search_query_draft")
        await self._send_text(
            chat_id,
            "✅ Черновик ИИ сохранён. Следующий поиск будет использовать эти запросы.\n"
            f"Файл настроек: {env_file}" + restart_note,
            reply_markup=self._menu_reply_markup(principal, menu=reply_menu),
        )
        return
