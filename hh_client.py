"""Playwright-based клиент для hh.ru — поиск, отклик, мониторинг приглашений."""
import os
import time
import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import urlencode
from playwright.async_api import async_playwright, BrowserContext, Page

try:
    from playwright_stealth import Stealth  # type: ignore
    _STEALTH_AVAILABLE = True
except ImportError:
    Stealth = None  # type: ignore
    _STEALTH_AVAILABLE = False

import config
from hh.apply import (
    CLOSED_OR_ARCHIVED_HH_COMPACT_MARKERS as _CLOSED_OR_ARCHIVED_HH_COMPACT_MARKERS,
    CLOSED_OR_ARCHIVED_HH_TEXT_MARKERS as _CLOSED_OR_ARCHIVED_HH_TEXT_MARKERS,
    apply_to_vacancy as _apply_to_vacancy,
    apply_success_detected as _apply_success_detected,
    click_with_fallbacks as _click_with_fallbacks,
    detect_response_controls as _detect_response_controls,
    dismiss_magritte_dropdowns as _dismiss_magritte_dropdowns,
    expand_cover_letter_input as _expand_cover_letter_input,
    fill_cover_letter_post_apply as _fill_cover_letter_post_apply,
    has_archived_hh_state as _has_archived_hh_state,
    has_existing_response_ui as _has_existing_response_ui,
    looks_like_closed_or_archived_hh as _looks_like_closed_or_archived_hh,
    looks_like_existing_hh_response as _looks_like_existing_hh_response,
    looks_like_hh_apply_success as _looks_like_hh_apply_success,
    page_closed_or_archived as _page_closed_or_archived,
    page_text as _page_text,
    response_requires_questions as _response_requires_questions,
    save_debug_snapshot as _save_debug_snapshot,
    submit_response_form_via_dom as _submit_response_form_via_dom,
)
from hh.browser import (
    HH_AUTH_COOKIE_NAMES,
    _ensure_dirs,
    _load_cookies,
    _save_cookies,
    has_auth_cookies as _has_browser_auth_cookies,
    save_session as _save_browser_session,
    start_browser as _start_browser,
    stop_browser as _stop_browser,
)
from hh.forms import (
    RISKY_QUESTION_PATTERNS,
    STABLE_ANSWER_LIBRARY,
    answer_choice_with_llm as _answer_choice_with_llm,
    answer_question_with_llm as _answer_question_with_llm,
    answer_question_from_library as _answer_question_from_library,
    extract_numeric_salary as _extract_numeric_salary,
    extract_resume_salary_text as _extract_resume_salary_text,
    fill_employer_question_answers as _fill_employer_question_answers,
    format_question_answer_note as _format_question_answer_note,
    inspect_employer_questions as _inspect_employer_questions,
    is_risky_question as _is_risky_question,
    is_salary_question as _is_salary_question,
    question_answer_item as _question_answer_item,
    submit_employer_questions as _submit_employer_questions,
    truncate_text as _truncate_text,
    try_auto_answer_questions as _try_auto_answer_questions,
)
from hh.resume import (
    _element_is_disabled,
    _element_text_summary,
    _find_resume_boost_action,
    _find_resume_boost_scope,
    _inspect_resume_boost,
    _looks_like_resume_boost_action,
    _looks_like_resume_boost_success,
    _looks_like_resume_boost_unavailable,
    _resume_matches_target,
    _save_resume_boost_debug,
    boost_resume as _boost_resume,
    get_resume_boost_status as _get_resume_boost_status,
    get_resume_ids as _get_resume_ids,
)
from hh.text import compact_text as _compact_text
from hh.text import normalize_text as _normalize_text
from llm_client import get_llm_client
import proxy_utils

log = logging.getLogger("hh_client")
_question_answer_client = None


def _absolute_hh_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"{config.HH_BASE_URL}{url}"


def _load_resume_text() -> str:
    try:
        if os.path.exists(config.RESUME_FILE):
            with open(config.RESUME_FILE, encoding="utf-8") as f:
                return f.read().strip()
    except Exception as exc:
        log.warning("Failed to read resume file %s: %s", config.RESUME_FILE, exc)
    return ""


from llm_utils import (
    parse_llm_json as _parse_llm_json,
    repair_llm_json as _repair_llm_json,
    strip_markdown_fence as _strip_markdown_fence,
)


from prompt_blocks import (  # noqa: E402
    build_salary_rule_block as _build_salary_rule_block,
    build_facts_block as _build_facts_block,
    build_profile_note_block as _build_profile_note_block,
    build_knowledge_base_block as _build_knowledge_base_block,
    build_filtered_kb_block as _build_filtered_kb_block,
)


def _anti_bot_label(kind: str) -> str:
    mapping = {
        "captcha": "captcha",
        "ddos_guard": "DDOS-GUARD",
        "browser_check": "проверка браузера",
        "rate_limit": "rate limit",
    }
    return mapping.get((kind or "").strip(), "anti-bot")


def _anti_bot_message(kind: str, suffix: str = "") -> str:
    message = f"hh.ru anti-bot ({_anti_bot_label(kind)})"
    if suffix:
        message = f"{message} {suffix}"
    return message


def _get_question_answer_client():
    global _question_answer_client
    if _question_answer_client is None:
        _question_answer_client = get_llm_client()
    return _question_answer_client


class HHClient:
    """Управляет браузерной сессией hh.ru."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._last_antibot_signal: dict | None = None

    async def start(self, headless: bool | None = None):
        """Запустить браузер и загрузить cookies."""
        return await _start_browser(
            self,
            headless,
            settings=config,
            playwright_factory=async_playwright,
            stealth_available=_STEALTH_AVAILABLE,
            stealth_factory=Stealth,
            proxy_env_builder=proxy_utils.browser_launch_env,
            ensure_dirs=_ensure_dirs,
            load_cookies=_load_cookies,
            logger=log,
        )

    async def stop(self):
        """Закрыть браузер."""
        return await _stop_browser(self, save_cookies=_save_cookies)

    def consume_antibot_signal(self) -> dict | None:
        signal = self._last_antibot_signal
        self._last_antibot_signal = None
        return signal

    def _remember_antibot_signal(self, kind: str, stage: str, message: str = "") -> None:
        self._last_antibot_signal = {
            "kind": (kind or "").strip(),
            "stage": (stage or "").strip(),
            "message": (message or _anti_bot_message(kind)).strip(),
            "url": (self._page.url if self._page else ""),
        }

    async def _click_with_fallbacks(self, element, label: str) -> bool:
        return await _click_with_fallbacks(self, element, label, logger=log)

    async def _has_existing_response_ui(self) -> bool:
        return await _has_existing_response_ui(
            self,
            looks_like_existing_response=_looks_like_existing_hh_response,
            logger=log,
        )

    async def _page_text(self, limit: int = 12000) -> str:
        return await _page_text(self, limit)

    async def _page_closed_or_archived(self) -> bool:
        return await _page_closed_or_archived(
            self,
            looks_like_closed_or_archived=_looks_like_closed_or_archived_hh,
            has_archived_state=_has_archived_hh_state,
        )

    async def _apply_success_detected(self) -> bool:
        return await _apply_success_detected(
            self,
            looks_like_apply_success=_looks_like_hh_apply_success,
            logger=log,
        )

    async def _response_requires_questions(self, current_url: str = "") -> bool:
        return await _response_requires_questions(self, current_url, logger=log)

    async def _inspect_employer_questions(self) -> dict:
        return await _inspect_employer_questions(self._page, logger=log)

    async def _fill_employer_question_answers(self, answers: list[dict]) -> dict:
        return await _fill_employer_question_answers(self._page, answers, logger=log)

    async def _submit_employer_questions(self) -> bool:
        return await _submit_employer_questions(
            self._page,
            submit_response_form_via_dom=self._submit_response_form_via_dom,
            click_with_fallbacks=self._click_with_fallbacks,
        )

    async def _answer_question_with_llm(
        self,
        field: dict,
        resume_text: str,
        page_text: str = "",
        vacancy_context: str = "",
    ) -> str | None:
        return await _answer_question_with_llm(
            field,
            resume_text,
            page_text,
            vacancy_context,
            settings=config,
            logger=log,
            get_question_answer_client=_get_question_answer_client,
            build_salary_rule_block=_build_salary_rule_block,
            build_facts_block=_build_facts_block,
            build_profile_note_block=_build_profile_note_block,
            build_filtered_kb_block=_build_filtered_kb_block,
            build_knowledge_base_block=_build_knowledge_base_block,
            parse_llm_json=_parse_llm_json,
            repair_llm_json=_repair_llm_json,
        )

    async def _answer_choice_with_llm(
        self,
        field: dict,
        resume_text: str,
        page_text: str = "",
        vacancy_context: str = "",
    ) -> dict | None:
        return await _answer_choice_with_llm(
            field,
            resume_text,
            page_text,
            vacancy_context,
            settings=config,
            logger=log,
            get_question_answer_client=_get_question_answer_client,
            build_salary_rule_block=_build_salary_rule_block,
            build_facts_block=_build_facts_block,
            build_profile_note_block=_build_profile_note_block,
            build_filtered_kb_block=_build_filtered_kb_block,
            build_knowledge_base_block=_build_knowledge_base_block,
            parse_llm_json=_parse_llm_json,
            repair_llm_json=_repair_llm_json,
        )

    async def _try_auto_answer_questions(self, vacancy_context: str = "") -> dict:
        return await _try_auto_answer_questions(
            self,
            vacancy_context,
            settings=config,
            load_resume_text=_load_resume_text,
            anti_bot_message=_anti_bot_message,
        )

    async def _dismiss_magritte_dropdowns(self) -> None:
        return await _dismiss_magritte_dropdowns(self)

    async def _expand_cover_letter_input(self) -> bool:
        return await _expand_cover_letter_input(self)

    async def _submit_response_form_via_dom(self) -> bool:
        return await _submit_response_form_via_dom(self, logger=log)

    async def _detect_anti_bot_kind(self) -> str:
        current_url = (self._page.url or "").lower()
        try:
            body_text = await self._page.evaluate(
                "() => document.body.innerText.slice(0, 3000)"
            )
        except Exception:
            body_text = ""
        if not isinstance(body_text, str):
            body_text = ""
        body_lower = body_text.lower()

        if (
            "ddos-guard" in current_url
            or "ddos-guard" in body_lower
            or "проверка браузера перед переходом на hh.ru" in body_lower
            or "не удалось проверить ваш браузер автоматически" in body_lower
            or "checking your browser before accessing" in body_lower
        ):
            return "ddos_guard"
        if "/account/captcha" in current_url:
            return "captcha"

        # Проверяем наличие iframe капчи (reCAPTCHA, hCaptcha, Yandex SmartCaptcha)
        try:
            captcha_frame = await self._page.query_selector(
                "iframe[src*='captcha'], "
                "iframe[src*='recaptcha'], "
                "iframe[src*='hcaptcha'], "
                "iframe[src*='smartcaptcha'], "
                "[class*='captcha' i], "
                "[id*='captcha' i], "
                "[data-qa='captcha']"
            )
            if captcha_frame:
                return "captcha"
        except Exception:
            pass

        if (
            "подтвердите, что вы не робот" in body_lower
            or "текст с картинки" in body_lower
            or "i'm not a robot" in body_lower
            or "verify you are human" in body_lower
        ):
            return "captcha"
        if (
            "проверка браузера" in body_lower
            or "checking your browser" in body_lower
            or "verify your browser" in body_lower
        ):
            return "browser_check"
        return ""

    async def _is_captcha_page(self) -> bool:
        return bool(await self._detect_anti_bot_kind())

    async def _handle_anti_bot_with_solver(self, kind: str, stage: str = "") -> str:
        """Тонкая обёртка над captcha_solver (R5)."""
        import captcha_solver
        return await captcha_solver.handle_anti_bot_with_solver(
            self, _get_question_answer_client, kind, stage
        )


    async def save_session(self):
        """Сохранить текущие cookies."""
        return await _save_browser_session(
            self,
            save_cookies=_save_cookies,
            logger=log,
        )

    async def has_auth_cookies(self) -> bool:
        """Проверить наличие auth-cookie без навигации страницы."""
        return await _has_browser_auth_cookies(
            self,
            base_url=config.HH_BASE_URL,
            auth_cookie_names=HH_AUTH_COOKIE_NAMES,
        )

    # ── Авторизация ───────────────────────────────────────────────────────

    async def login_interactive(self, keep_open: bool = False):
        """
        Открыть браузер для ручного логина.
        Пользователь логинится сам, потом нажимаем Enter в терминале.
        Если keep_open=True — не закрывает браузер (для последующей загрузки резюме).
        """
        await self.start(headless=False)
        try:
            await self._page.goto(
                f"{config.HH_BASE_URL}/account/login",
                wait_until="domcontentloaded",
                timeout=60000,
            )
        except Exception:
            # Даже если таймаут — страница могла частично загрузиться, продолжаем
            pass
        print("\n" + "=" * 60)
        print("Браузер открыт. Залогинься на hh.ru.")
        print("После успешного входа нажми Enter здесь...")
        print("=" * 60)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, input)
        await self.save_session()
        print("✅ Cookies сохранены!")
        if not keep_open:
            await self.stop()

    async def google_login_interactive(self, form_url: str = ""):
        """Открыть Playwright-браузер для ручного Google login и сохранить cookies."""
        await self.start(headless=False)
        target_url = form_url or "https://accounts.google.com/"
        try:
            await self._page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        print("\n" + "=" * 60)
        print("Браузер открыт. Войди в Google в этом Playwright-окне.")
        if form_url:
            print("После входа проверь, что форма доступна для заполнения.")
        print("После успешного входа нажми Enter здесь...")
        print("=" * 60)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, input)
        await self.save_session()
        print("✅ Google cookies сохранены в профильный браузерный контекст!")
        await self.stop()

    async def is_logged_in(self) -> bool:
        """Проверить залогинен ли пользователь."""
        try:
            await self._page.goto(f"{config.HH_BASE_URL}/applicant/resumes", wait_until="domcontentloaded", timeout=30000)
            await self._page.wait_for_timeout(2000)
            # Закрыть модалку "Резюме стали компактнее" (whats-new-modal) если есть
            await self._dismiss_whats_new_modal()
            url = (self._page.url or "").casefold()
            # Если редиректнуло на логин — не залогинен
            if "/account/login" in url or "/auth/" in url:
                return False
            html = _compact_text(await self._page.content())
            if '"usertype":"anonymous"' in html or '"luxpagename":"forbiddenpage"' in html:
                return False
            # Проверяем наличие элемента резюме (новый дизайн: resume-card-link-*)
            resumes = await self._page.query_selector_all("[data-qa='resume'], [data-qa^='resume-card-link-']")
            return len(resumes) > 0 or "/applicant/resumes" in url
        except Exception as e:
            log.warning("Login check failed: %s", e)
            return False

    async def _dismiss_whats_new_modal(self):
        """Закрыть модалку 'Резюме стали компактнее' (whats-new-modal) если появилась."""
        try:
            btn = await self._page.query_selector("[data-qa='whats-new-modal-confirm']")
            if btn:
                await btn.click()
                await self._page.wait_for_timeout(500)
                log.info("Dismissed whats-new-modal popup")
        except Exception:
            pass

    async def is_logged_in_passive(self) -> bool:
        """Проверить логин без навигации текущей страницы."""
        if not self._page or self._page.is_closed():
            return False
        try:
            url = (self._page.url or "").casefold()
            if not url or "/account/login" in url or "/auth/" in url or "/captcha" in url:
                return False
            html = _compact_text(await self._page.content())
            if '"usertype":"anonymous"' in html or '"luxpagename":"forbiddenpage"' in html:
                return False
            return await self.has_auth_cookies()
        except Exception as e:
            log.warning("Passive login check failed: %s", e)
            return False

    # ── Поиск вакансий ────────────────────────────────────────────────────

    async def search_vacancies(self, query: str, page: int = 0,
                              area: int = 113, schedule: str = "") -> list[dict]:
        """
        Поиск вакансий по запросу. Возвращает список:
        [{"id": "...", "title": "...", "company": "...", "salary": "...",
          "url": "...", "snippet": "..."}]
        """
        params = {
            "text": query,
            "area": area,
            "page": page,
            "per_page": 20,
            "order_by": "publication_time",  # свежие первые
        }
        if config.SEARCH_EXPERIENCE:
            params["experience"] = config.SEARCH_EXPERIENCE
        if schedule:
            params["schedule"] = schedule
        if config.SEARCH_SALARY:
            params["salary"] = config.SEARCH_SALARY
        if config.SEARCH_ONLY_WITH_SALARY:
            params["only_with_salary"] = "true"

        url = f"{config.HH_BASE_URL}/search/vacancy?{urlencode(params)}"
        log.info("Searching: %s", url)

        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log.warning("Search page nav issue: %s", e)

        await self._page.wait_for_timeout(4000)  # дать JS подгрузиться

        # Дебаг: скриншот поисковой выдачи (первый запрос)
        if page == 0:
            try:
                safe_query = "".join(c if c.isalnum() else "_" for c in query[:20])
                debug_path = os.path.join(config.HH_STATE_DIR, f"debug_search_{safe_query}.png")
                await self._page.screenshot(path=debug_path, full_page=True)
                debug_html = os.path.join(config.HH_STATE_DIR, f"debug_search_{safe_query}.html")
                html = await self._page.content()
                with open(debug_html, "w") as f:
                    f.write(html)
                log.info("Search debug saved: %s", debug_path)
            except Exception:
                pass

        # Anti-bot check перед парсингом
        anti_bot_kind = await self._detect_anti_bot_kind()
        if anti_bot_kind:
            message = _anti_bot_message(anti_bot_kind, "на поиске")
            self._remember_antibot_signal(anti_bot_kind, "search", message)
            log.warning("hh.ru anti-bot (%s) on search page: %s", anti_bot_kind, self._page.url)
            return []

        vacancies = []

        # Стратегия 1: data-qa селекторы (классический hh.ru)
        cards = await self._page.query_selector_all("[data-qa='serp-item']")
        log.info("Search strategy 1 (serp-item): %d cards", len(cards))

        # Стратегия 2: альтернативные селекторы
        if not cards:
            cards = await self._page.query_selector_all("[data-qa='vacancy-serp__vacancy']")
            log.info("Search strategy 2 (vacancy-serp__vacancy): %d cards", len(cards))

        # Стратегия 3: любые карточки с ссылкой на вакансию
        if not cards:
            cards = await self._page.query_selector_all("[data-qa*='serp-item'], [data-qa*='vacancy-serp']")
            log.info("Search strategy 3 (wildcard serp): %d cards", len(cards))

        # Стратегия 4: ищем по ссылкам на /vacancy/
        if not cards:
            log.info("All card strategies failed, falling back to link parsing")
            vacancy_links = await self._page.query_selector_all("a[href*='/vacancy/']")
            log.info("Found %d vacancy links on page", len(vacancy_links))
            seen_ids = set()
            for link in vacancy_links:
                try:
                    href = await link.get_attribute("href") or ""
                    if "/vacancy/" not in href:
                        continue
                    vid = href.split("/vacancy/")[-1].split("?")[0].split("/")[0]
                    if not vid or not vid.isdigit() or vid in seen_ids:
                        continue
                    seen_ids.add(vid)
                    title = (await link.inner_text()).strip()
                    if not title or len(title) < 3 or len(title) > 200:
                        continue
                    # Пытаемся найти родительский контейнер для доп. инфо
                    parent = await link.evaluate_handle("el => el.closest('[class*=\"vacancy\"], [class*=\"serp\"], article, section') || el.parentElement.parentElement")
                    company = ""
                    salary = "не указана"
                    snippet = ""
                    if parent:
                        full_text = await parent.evaluate("el => el.innerText")
                        lines = [l.strip() for l in full_text.split("\n") if l.strip()]
                        # Обычно: заголовок, зарплата, компания, ...
                        for line in lines:
                            if "₽" in line or "$" in line or "руб" in line.lower():
                                salary = line
                            elif line != title and not company and len(line) > 2:
                                company = line
                    vacancies.append({
                        "id": vid,
                        "title": title,
                        "company": company,
                        "salary": salary,
                        "url": href if href.startswith("http") else f"{config.HH_BASE_URL}{href}",
                        "snippet": snippet,
                    })
                except Exception as e:
                    log.debug("Link parse failed: %s", e)

            log.info("Found %d vacancies via link parsing for '%s'", len(vacancies), query)
            return vacancies

        for card in cards:
            try:
                vacancy = await self._parse_vacancy_card(card)
                if vacancy:
                    vacancies.append(vacancy)
            except Exception as e:
                log.debug("Failed to parse vacancy card: %s", e)

        log.info("Found %d vacancies for '%s'", len(vacancies), query)
        return vacancies

    async def _parse_vacancy_card(self, card) -> dict | None:
        """Парсит карточку вакансии из поисковой выдачи."""
        # Заголовок и ссылка
        title_el = await card.query_selector(
            "[data-qa='serp-item__title'], "
            "[data-qa='serp__vacancy-title'], "
            "a.serp-item__title, "
            "h2 a, h3 a"
        )
        if not title_el:
            return None

        title = (await title_el.inner_text()).strip()
        url = _absolute_hh_url(await title_el.get_attribute("href") or "")

        # Извлекаем ID из URL
        vacancy_id = ""
        if "/vacancy/" in url:
            parts = url.split("/vacancy/")
            if len(parts) > 1:
                vacancy_id = parts[1].split("?")[0].split("/")[0]

        # Компания
        company_el = await card.query_selector(
            "[data-qa='vacancy-serp__vacancy-employer'], "
            "[data-qa='serp-item__company'], "
            ".vacancy-serp-item__meta-info-company a"
        )
        company = (await company_el.inner_text()).strip() if company_el else "—"

        # Зарплата
        salary_el = await card.query_selector(
            "[data-qa='vacancy-serp__vacancy-compensation'], "
            "[data-qa='serp-item__compensation'], "
            ".vacancy-serp-item__sidebar"
        )
        salary = (await salary_el.inner_text()).strip() if salary_el else "не указана"

        # Сниппет (краткое описание)
        snippet_el = await card.query_selector(
            "[data-qa='vacancy-serp__vacancy_snippet_requirement'], "
            ".g-user-content"
        )
        snippet = (await snippet_el.inner_text()).strip() if snippet_el else ""

        # Дополнительный сниппет (обязанности)
        resp_el = await card.query_selector(
            "[data-qa='vacancy-serp__vacancy_snippet_responsibility']"
        )
        if resp_el:
            resp_text = (await resp_el.inner_text()).strip()
            if resp_text:
                snippet = f"{snippet}\n{resp_text}" if snippet else resp_text

        response_el = await card.query_selector(
            "[data-qa='vacancy-serp__vacancy_response'], "
            "a[href*='/applicant/vacancy_response']"
        )
        response_url = _absolute_hh_url(
            await response_el.get_attribute("href") or ""
        ) if response_el else ""

        return {
            "id": vacancy_id,
            "title": title,
            "company": company,
            "salary": salary,
            "url": url,
            "snippet": snippet,
            "response_url": response_url,
        }

    # ── Детали вакансии ───────────────────────────────────────────────────

    async def get_vacancy_details(self, vacancy_url: str) -> str:
        """Получить полный текст вакансии."""
        await self._page.goto(vacancy_url, wait_until="domcontentloaded", timeout=20000)
        await self._page.wait_for_timeout(2000)

        body_text = await self._page_text(limit=20000)
        try:
            page_html = await self._page.content()
        except Exception:
            page_html = ""
        if _looks_like_closed_or_archived_hh(body_text) or _has_archived_hh_state(page_html):
            status = "Вакансия закрыта или находится в архиве"
            context = (body_text or page_html).strip()[:3000]
            return f"{status}\n\n{context}" if context else status

        # Описание вакансии
        desc_el = await self._page.query_selector(
            "[data-qa='vacancy-description'], "
            ".vacancy-description, "
            ".vacancy-section"
        )
        if desc_el:
            return (await desc_el.inner_text()).strip()

        # Fallback: весь контент страницы
        body = await self._page.query_selector("main, .vacancy-body, article")
        if body:
            return (await body.inner_text()).strip()[:3000]

        return ""

    # ── Отклик на вакансию ────────────────────────────────────────────────

    async def _save_debug_snapshot(self, prefix: str) -> None:
        return await _save_debug_snapshot(
            self,
            prefix,
            state_dir=config.HH_STATE_DIR,
        )

    async def _detect_response_controls(self):
        return await _detect_response_controls(self)

    async def apply_to_vacancy(
        self,
        vacancy_url: str,
        cover_letter: str = "",
        response_url: str = "",
        preferred_resume_title: str = "",
        preferred_resume_id: str = "",
        vacancy_context: str = "",
    ) -> dict:
        return await _apply_to_vacancy(
            self,
            vacancy_url,
            cover_letter,
            response_url,
            preferred_resume_title,
            preferred_resume_id,
            vacancy_context,
            absolute_hh_url=_absolute_hh_url,
            anti_bot_message=_anti_bot_message,
            logger=log,
        )

    async def _fill_cover_letter_post_apply(self, cover_letter: str):
        return await _fill_cover_letter_post_apply(
            self,
            cover_letter,
            logger=log,
        )

    # ── Проверка откликов / приглашений ───────────────────────────────────

    async def check_negotiations(self) -> dict:
        """
        Проверить статус откликов.
        Возвращает {"invitations": [...], "responses": int, "new_messages": int}
        """
        await self._page.goto(
            f"{config.HH_BASE_URL}/applicant/negotiations",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await self._page.wait_for_timeout(3000)

        result = {"invitations": [], "responses": 0, "new_messages": 0}

        # Считаем общее количество откликов
        try:
            tabs = await self._page.query_selector_all("[data-qa*='negotiations__tab']")
            for tab in tabs:
                text = (await tab.inner_text()).strip().lower()
                # Вытаскиваем число из текста вкладки
                import re
                nums = re.findall(r"\d+", text)
                if "приглашен" in text and nums:
                    # Вкладка приглашений
                    pass
        except Exception:
            pass

        # Кликаем на вкладку "Приглашения"
        invite_tab = await self._page.query_selector(
            "[data-qa='negotiations__tab_invitation'], "
            "a[href*='invitation']"
        )
        if invite_tab:
            await invite_tab.click()
            await self._page.wait_for_timeout(2000)

            invite_cards = await self._page.query_selector_all(
                "[data-qa='negotiations-item'], "
                ".negotiations-item, "
                ".resume-negotiations-item"
            )
            for card in invite_cards:
                try:
                    title_el = await card.query_selector("a[data-qa*='title'], h3 a, a")
                    if title_el:
                        title = (await title_el.inner_text()).strip()
                        href = await title_el.get_attribute("href") or ""
                        company_el = await card.query_selector(
                            "[data-qa*='employer'], .negotiations-item__company"
                        )
                        company = (await company_el.inner_text()).strip() if company_el else "—"
                        result["invitations"].append({
                            "title": title,
                            "company": company,
                            "url": href,
                        })
                except Exception:
                    pass

        return result

    async def get_negotiation_statuses(self) -> list[dict]:
        """Прочитать видимые статусы откликов на странице переговоров."""
        await self._page.goto(
            f"{config.HH_BASE_URL}/applicant/negotiations",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await self._page.wait_for_timeout(3000)

        items = []
        cards = await self._page.query_selector_all(
            "[data-qa='negotiations-item'], "
            ".negotiations-item, "
            ".resume-negotiations-item"
        )
        for card in cards:
            try:
                text = (await card.inner_text()).strip()
                if not text:
                    continue
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                if len(lines) < 3:
                    continue
                link = await card.query_selector("a[href*='/vacancy/']")
                href = await link.get_attribute("href") if link else ""
                href = _absolute_hh_url(href or "")
                vacancy_id = ""
                if "/vacancy/" in href:
                    vacancy_id = href.split("/vacancy/")[-1].split("?")[0].split("/")[0]
                items.append(
                    {
                        "id": vacancy_id,
                        "status": lines[0],
                        "title": lines[1],
                        "company": lines[2],
                        "url": href,
                    }
                )
            except Exception:
                pass
        return items

    # ── Получить список резюме ────────────────────────────────────────────

    async def get_resume_ids(self) -> list[dict]:
        """Получить ID резюме пользователя."""
        return await _get_resume_ids(
            self,
            anti_bot_message=_anti_bot_message,
            settings=config,
            logger=log,
        )

    async def _save_resume_boost_debug(self, stage: str) -> dict:
        return await _save_resume_boost_debug(
            self,
            stage,
            settings=config,
            logger=log,
        )

    async def _element_text_summary(self, element) -> str:
        return await _element_text_summary(element)

    async def _element_is_disabled(self, element) -> bool:
        return await _element_is_disabled(element)

    async def _find_resume_boost_scope(
        self,
        resume_id: str = "",
        resume_title: str = "",
    ):
        return await _find_resume_boost_scope(
            self,
            resume_id,
            resume_title,
            normalize_text=_normalize_text,
        )

    async def _find_resume_boost_action(
        self,
        resume_id: str = "",
        resume_title: str = "",
    ) -> dict:
        return await _find_resume_boost_action(
            self,
            resume_id,
            resume_title,
            looks_like_action=_looks_like_resume_boost_action,
        )

    async def _inspect_resume_boost(
        self,
        resume_id: str = "",
        resume_title: str = "",
    ) -> dict:
        return await _inspect_resume_boost(
            self,
            resume_id,
            resume_title,
            resume_matches_target=_resume_matches_target,
            looks_like_unavailable=_looks_like_resume_boost_unavailable,
        )

    async def get_resume_boost_status(
        self,
        resume_id: str = "",
        resume_title: str = "",
    ) -> dict:
        """Проверить наличие кнопки поднятия резюме без клика."""
        return await _get_resume_boost_status(self, resume_id, resume_title)

    async def boost_resume(
        self,
        resume_id: str = "",
        resume_title: str = "",
        confirm: str = "",
    ) -> dict:
        """Поднять резюме вручную. Требует env-флаг и явное слово подтверждения."""
        return await _boost_resume(
            self,
            resume_id,
            resume_title,
            confirm,
            settings=config,
            looks_like_success=_looks_like_resume_boost_success,
        )

    # ── Скачать полное резюме ─────────────────────────────────────────────

    async def download_resume(self) -> dict:
        """Скачать первое резюме. Для выбора используй download_resume_by_id."""
        resumes = await self.get_resume_ids()
        if not resumes:
            log.error("No resumes found on listing page")
            log.error("Check debug: %s/debug_resumes.png", config.HH_STATE_DIR)
            return {"title": "", "sections": {}, "raw": ""}
        return await self.download_resume_by_id(resumes[0])

    async def download_resume_by_id(self, resume: dict) -> dict:
        """
        Скачать конкретное резюме.
        resume: {"id": str, "title": str, "url": str}
        Возвращает {"title": str, "sections": {name: text}, "raw": str}
        """
        resume_url = resume.get("url", "")
        if not resume_url:
            resume_url = f"{config.HH_BASE_URL}/resume/{resume['id']}"
        if not resume_url.startswith("http"):
            resume_url = f"{config.HH_BASE_URL}{resume_url}"

        log.info("Downloading resume: %s (%s)", resume["title"], resume_url)

        try:
            await self._page.goto(resume_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log.warning("Resume page nav issue: %s", e)

        await self._page.wait_for_timeout(4000)

        # Скроллим вниз чтобы подгрузить lazy-loaded блоки
        await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await self._page.wait_for_timeout(2000)
        await self._page.evaluate("window.scrollTo(0, 0)")
        await self._page.wait_for_timeout(1000)

        # Дебаг скриншот страницы резюме
        try:
            debug_path = os.path.join(config.HH_STATE_DIR, "debug_resume_page.png")
            await self._page.screenshot(path=debug_path, full_page=True)
            debug_html = os.path.join(config.HH_STATE_DIR, "debug_resume_page.html")
            html = await self._page.content()
            with open(debug_html, "w") as f:
                f.write(html)
            log.info("Resume page debug saved: %s", debug_path)
        except Exception:
            pass

        sections = {}

        # Заголовок (должность)
        title_el = await self._page.query_selector("[data-qa='resume-block-title-position']")
        title = (await title_el.inner_text()).strip() if title_el else resume["title"]

        # Зарплатные ожидания
        salary_el = await self._page.query_selector("[data-qa='resume-block-salary']")
        if salary_el:
            sections["Зарплата"] = (await salary_el.inner_text()).strip()

        # Позиция (формат работы, занятость, командировки)
        position_card = await self._page.query_selector("[data-qa='resume-position-card']")
        if position_card:
            pos_text = (await position_card.inner_text()).strip()
            if pos_text:
                sections["Позиция"] = pos_text

        # Опыт работы
        exp_card = await self._page.query_selector("[data-qa='resume-list-card-experience']")
        if exp_card:
            exp_text = (await exp_card.inner_text()).strip()
            if exp_text:
                sections["Опыт работы"] = exp_text

        # Навыки (карточка)
        skills_card = await self._page.query_selector("[data-qa='skills-card']")
        if skills_card:
            skills_text = (await skills_card.inner_text()).strip()
            if skills_text:
                sections["Навыки"] = skills_text

        # Подтверждённые навыки / методы
        skills_methods = await self._page.query_selector("[data-qa='skills-methods']")
        if skills_methods:
            sm_text = (await skills_methods.inner_text()).strip()
            if sm_text:
                sections["Подтверждение навыков"] = sm_text

        # Образование
        edu_card = await self._page.query_selector("[data-qa='resume-list-card-education']")
        if edu_card:
            edu_text = (await edu_card.inner_text()).strip()
            if edu_text:
                sections["Образование"] = edu_text

        # О себе
        about_card = await self._page.query_selector("[data-qa='resume-about-card']")
        if about_card:
            about_text = (await about_card.inner_text()).strip()
            if about_text:
                sections["О себе"] = about_text

        # Fallback: если мало секций — парсим все карточки на странице
        if len(sections) < 3:
            log.info("Few sections found (%d), trying fallback parser", len(sections))
            all_cards = await self._page.query_selector_all("[data-qa$='-card']")
            for card in all_cards:
                qa = await card.get_attribute("data-qa") or ""
                if qa in ("resume-position-card", "resume-about-card",
                          "skills-card", "resume-visibility-card",
                          "resume-list-card-experience",
                          "resume-list-card-education"):
                    continue  # уже обработали или не нужно
                text = (await card.inner_text()).strip()
                if text and len(text) > 20:
                    name = qa.replace("resume-", "").replace("-card", "").replace("-", " ").title()
                    sections[name] = text

        # Собираем в markdown
        raw = f"# {title}\n\n"
        for name, text in sections.items():
            raw += f"## {name}\n{text}\n\n"

        log.info("Downloaded resume: %s (%d sections)", title, len(sections))
        return {"title": title, "sections": sections, "raw": raw}
