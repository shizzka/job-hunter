"""Playwright-based клиент для hh.ru — поиск, отклик, мониторинг приглашений."""
import json
import os
import asyncio
import logging
from pathlib import Path
from urllib.parse import urlencode
from playwright.async_api import async_playwright, BrowserContext, Page

import config

log = logging.getLogger("hh_client")


def _ensure_dirs():
    os.makedirs(os.path.dirname(config.HH_COOKIES_FILE), exist_ok=True)
    os.makedirs(config.HH_STATE_DIR, exist_ok=True)


def _load_cookies() -> list[dict] | None:
    if os.path.exists(config.HH_COOKIES_FILE):
        with open(config.HH_COOKIES_FILE) as f:
            return json.load(f)
    return None


def _save_cookies(cookies: list[dict]):
    _ensure_dirs()
    with open(config.HH_COOKIES_FILE, "w") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)


def _absolute_hh_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"{config.HH_BASE_URL}{url}"


def _normalize_text(value: str) -> str:
    return " ".join((value or "").split()).casefold()


class HHClient:
    """Управляет браузерной сессией hh.ru."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def start(self, headless: bool | None = None):
        """Запустить браузер и загрузить cookies."""
        _ensure_dirs()
        self._pw = await async_playwright().start()

        launch_opts = {
            "headless": headless if headless is not None else config.HEADLESS,
            "slow_mo": config.SLOW_MO,
        }
        # Прокси (Mihomo на 127.0.0.1:7897) — если hh.ru не грузится напрямую
        proxy_url = os.environ.get("HH_PROXY", config.BROWSER_PROXY)
        if proxy_url:
            launch_opts["proxy"] = {"server": proxy_url}
            log.info("Using proxy: %s", proxy_url)

        self._browser = await self._pw.chromium.launch(**launch_opts)
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
        )
        cookies = _load_cookies()
        if cookies:
            await self._context.add_cookies(cookies)
            log.info("Loaded %d cookies", len(cookies))
        self._page = await self._context.new_page()

    async def stop(self):
        """Закрыть браузер."""
        if self._context:
            cookies = await self._context.cookies()
            _save_cookies(cookies)
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def _click_with_fallbacks(self, element, label: str) -> bool:
        """Надёжный клик по элементу с fallback-стратегиями."""
        if not element:
            return False

        try:
            await element.evaluate(
                "el => el.scrollIntoView({block: 'center', inline: 'center'})"
            )
            await self._page.wait_for_timeout(300)
        except Exception:
            pass

        strategies = (
            ("normal", lambda: element.click(timeout=5000)),
            ("force", lambda: element.click(timeout=5000, force=True)),
            (
                "js",
                lambda: element.evaluate(
                    "el => { el.scrollIntoView({block: 'center', inline: 'center'}); el.click(); }"
                ),
            ),
        )

        for strategy_name, action in strategies:
            try:
                log.info("Clicking %s via %s strategy", label, strategy_name)
                await action()
                await self._page.wait_for_timeout(1000)
                return True
            except Exception as e:
                log.warning("%s click via %s failed: %s", label, strategy_name, e)

        return False

    async def _is_captcha_page(self) -> bool:
        current_url = (self._page.url or "").lower()
        if "/account/captcha" in current_url:
            return True

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
                return True
        except Exception:
            pass

        try:
            body_text = await self._page.evaluate(
                "() => document.body.innerText.slice(0, 2000)"
            )
        except Exception:
            return False

        body_lower = body_text.lower()
        return (
            "подтвердите, что вы не робот" in body_lower
            or "текст с картинки" in body_lower
            or "i'm not a robot" in body_lower
            or "verify you are human" in body_lower
        )

    async def save_session(self):
        """Сохранить текущие cookies."""
        if self._context:
            cookies = await self._context.cookies()
            _save_cookies(cookies)
            log.info("Session saved (%d cookies)", len(cookies))

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

    async def is_logged_in(self) -> bool:
        """Проверить залогинен ли пользователь."""
        try:
            await self._page.goto(f"{config.HH_BASE_URL}/applicant/resumes", wait_until="domcontentloaded", timeout=15000)
            await self._page.wait_for_timeout(2000)
            url = self._page.url
            # Если редиректнуло на логин — не залогинен
            if "/account/login" in url or "/auth/" in url:
                return False
            # Проверяем наличие элемента резюме
            resumes = await self._page.query_selector_all("[data-qa='resume']")
            return len(resumes) > 0 or "resumes" in url
        except Exception as e:
            log.warning("Login check failed: %s", e)
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

        # Captcha check перед парсингом
        if await self._is_captcha_page():
            log.warning("hh.ru captcha on search page: %s", self._page.url)
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

    async def apply_to_vacancy(
        self,
        vacancy_url: str,
        cover_letter: str = "",
        response_url: str = "",
        preferred_resume_title: str = "",
        preferred_resume_id: str = "",
    ) -> dict:
        """
        Откликнуться на вакансию.
        Возвращает {"ok": bool, "message": str}
        """
        vacancy_url = _absolute_hh_url(vacancy_url)
        response_url = _absolute_hh_url(response_url)

        async def save_debug_snapshot(prefix: str):
            try:
                debug_path = os.path.join(config.HH_STATE_DIR, f"{prefix}.png")
                debug_html = os.path.join(config.HH_STATE_DIR, f"{prefix}.html")
                await self._page.screenshot(path=debug_path)
                with open(debug_html, "w") as f:
                    f.write(await self._page.content())
            except Exception:
                pass

        async def detect_response_controls():
            current_url = self._page.url
            questions_header = await self._page.query_selector(
                "h1:has-text('Отклик на вакансию'), "
                "h1:has-text('Ответьте на вопросы')"
            )
            questions_form = await self._page.query_selector(
                "text='Ответьте на вопросы'"
            )
            resume_select = await self._page.query_selector(
                "[data-qa='resume-select'], "
                "[data-qa*='resume-item'], "
                "[data-qa='vacancy-response-popup-form-resume']"
            )
            letter_field = await self._page.query_selector(
                "[data-qa='vacancy-response-popup-form-letter-input'], "
                "textarea[name='letter'], "
                "textarea[data-qa*='letter'], "
                ".vacancy-response-popup textarea, "
                "textarea"
            )
            submit_btn = await self._page.query_selector(
                "[data-qa='vacancy-response-submit-popup'], "
                "[data-qa='vacancy-response-letter-submit'], "
                "button[data-qa*='submit']"
            )
            if not submit_btn:
                submit_btn = await self._page.query_selector(
                    "button:has-text('Откликнуться'), "
                    "button:has-text('Отправить')"
            )
            return (
                current_url,
                questions_header,
                questions_form,
                resume_select,
                letter_field,
                submit_btn,
            )

        async def select_preferred_resume() -> bool:
            title_norm = _normalize_text(preferred_resume_title)
            id_norm = (preferred_resume_id or "").strip()

            if not resume_select and not title_norm and not id_norm:
                return True

            async def collect_resume_items():
                return await self._page.query_selector_all(
                    "[data-magritte-select-option], "
                    "[data-qa^='magritte-select-option-'], "
                    "[data-qa*='resume-item'], "
                    "[data-qa='vacancy-response-popup-form-resume'], "
                    "[data-qa='resume-select'] [role='button'][tabindex='0'], "
                    "[data-qa='resume-select'] [data-qa='cell'], "
                    "[data-qa='resume-select'] label, "
                    "label[data-qa='cell'], "
                    "[data-qa='resume-title'], "
                    "[data-qa='resume-detail'], "
                    "[data-qa='cell-text-content']"
                )

            async def has_resume_choices() -> bool:
                resume_items = await collect_resume_items()
                seen_texts = set()
                meaningful = 0
                for item in resume_items:
                    try:
                        text = _normalize_text(await item.inner_text())
                    except Exception:
                        continue
                    if not text or text in seen_texts:
                        continue
                    seen_texts.add(text)
                    if len(text) >= 6:
                        meaningful += 1
                    if meaningful >= 2:
                        return True
                return False

            async def expand_resume_picker():
                toggles = [
                    "[data-qa='resume-select'] [role='button'][tabindex='0']",
                    "[role='dialog'] [role='button'][tabindex='0']",
                    "form[name='vacancy_response'] [role='button'][tabindex='0']",
                    "[data-qa='vacancy-response-popup-form-resume']",
                    "[data-qa='resume-select'] [data-qa='cell']",
                    "[data-qa='resume-title']",
                    "[data-qa='resume-detail']",
                    "[data-qa='cell']",
                ]
                for selector in toggles:
                    handle = await self._page.query_selector(selector)
                    if not handle:
                        continue
                    if await self._click_with_fallbacks(handle, f"resume_toggle:{selector}"):
                        await self._page.wait_for_timeout(1000)
                        if await has_resume_choices():
                            return True
                return False

            if resume_select:
                await self._click_with_fallbacks(resume_select, "resume_select")
                await self._page.wait_for_timeout(1000)

            resume_items = await collect_resume_items()

            if not resume_items and (title_norm or id_norm):
                page_text = _normalize_text(
                    await self._page.evaluate("() => document.body.innerText.slice(0, 4000)")
                )
                if (title_norm and title_norm in page_text) or (id_norm and id_norm in page_text):
                    return True
                return False

            if not title_norm and not id_norm:
                if resume_items:
                    return await self._click_with_fallbacks(resume_items[0], "resume_item_default")
                return True

            async def find_matching_item(items):
                for item in items:
                    try:
                        text = _normalize_text(await item.inner_text())
                    except Exception:
                        continue
                    if not text:
                        continue
                    if id_norm and id_norm in text:
                        return item
                    if title_norm and (title_norm in text or text in title_norm):
                        return item
                return None

            best_item = await find_matching_item(resume_items)

            # Single-resume shortcut: если на странице ровно одно резюме
            # и пикер не раскрывается — считаем его выбранным
            if best_item is None and resume_items:
                unique_texts = set()
                for item in resume_items:
                    try:
                        text = _normalize_text(await item.inner_text())
                        if text and len(text) >= 6:
                            unique_texts.add(text)
                    except Exception:
                        continue
                if len(unique_texts) <= 1:
                    log.info("Single resume on page — treating as selected")
                    return True

            if best_item is None:
                expanded = await expand_resume_picker()
                if expanded:
                    resume_items = await collect_resume_items()
                    best_item = await find_matching_item(resume_items)

            if best_item is None:
                # Последняя попытка: проверить текст страницы
                page_text = _normalize_text(
                    await self._page.evaluate("() => document.body.innerText.slice(0, 6000)")
                )
                if title_norm and title_norm in page_text:
                    log.info("Resume title found in page text — treating as selected")
                    return True
                return False

            return await self._click_with_fallbacks(best_item, "resume_item_preferred")

        try:
            await self._page.goto(vacancy_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log.warning("Vacancy page nav issue: %s", e)

        await self._page.wait_for_timeout(3000)
        await save_debug_snapshot("debug_apply_page")

        if await self._is_captcha_page():
            log.warning("hh.ru captcha encountered on vacancy page: %s", self._page.url)
            if response_url and response_url != vacancy_url:
                log.info("Retrying apply flow via direct response URL: %s", response_url)
                try:
                    await self._page.goto(
                        response_url,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                except Exception as e:
                    log.warning("Direct response page nav issue: %s", e)
                await self._page.wait_for_timeout(3000)
                await save_debug_snapshot("debug_apply_response_page")
            if await self._is_captcha_page():
                return {"ok": False, "message": "hh.ru показал captcha"}

        # Ищем кнопку "Откликнуться" — собираем все data-qa для дебага
        apply_btn = await self._page.query_selector(
            "[data-qa='vacancy-response-link-top-again'], "
            "[data-qa='vacancy-response-link-bottom-again'], "
            "[data-qa='vacancy-response-link-top'], "
            "[data-qa='vacancy-response-link-bottom'], "
            "a[data-qa*='response-link'], "
            "button[data-qa*='vacancy-response']"
        )

        if not apply_btn:
            # Кнопка повторного отклика другим резюме
            apply_btn = await self._page.query_selector(
                "button:has-text('Отклик другим резюме'), "
                "a:has-text('Отклик другим резюме')"
            )

        if not apply_btn:
            # Попробуем найти по тексту
            apply_btn = await self._page.query_selector(
                "button:has-text('Откликнуться'), "
                "a:has-text('Откликнуться')"
            )

        if not apply_btn:
            # Возможно уже откликались
            already = await self._page.query_selector(
                "[data-qa*='responded'], "
                "button:has-text('Вы откликнулись'), "
                "a:has-text('Вы откликнулись')"
            )
            if already:
                return {"ok": True, "message": "Уже откликались ранее"}

            (
                current_url,
                questions_header,
                questions_form,
                resume_select,
                letter_field,
                submit_btn,
            ) = await detect_response_controls()
            direct_response_flow = (
                "/applicant/vacancy_response" in current_url
                or resume_select is not None
                or letter_field is not None
                or submit_btn is not None
            )

            if questions_header or questions_form or "vacancy_response_question" in current_url:
                log.info("Vacancy requires employer questions — skipping")
                return {"ok": False, "message": "Требуются доп. вопросы работодателя — пропускаем"}

            if direct_response_flow:
                log.info("Direct response flow detected without initial vacancy button")
            else:
                # Дебаг: какие data-qa есть на странице
                qa_attrs = await self._page.evaluate(
                    "() => [...document.querySelectorAll('[data-qa]')].map(el => el.getAttribute('data-qa')).filter(a => a.includes('response') || a.includes('vacanc')).slice(0, 20)"
                )
                log.warning("Apply button not found. Relevant data-qa: %s", qa_attrs)
                return {"ok": False, "message": f"Кнопка не найдена. qa={qa_attrs[:5]}"}
        else:
            direct_response_flow = False

        if not direct_response_flow:
            log.info("Found apply button, clicking...")
            await apply_btn.scroll_into_view_if_needed()
            await self._page.wait_for_timeout(300)
            await apply_btn.click()
            await self._page.wait_for_timeout(3000)
            await save_debug_snapshot("debug_apply_after_click")

        (
            current_url,
            questions_header,
            questions_form,
            resume_select,
            letter_field,
            submit_btn,
        ) = await detect_response_controls()

        if questions_header or questions_form or "vacancy_response_question" in current_url:
            log.info("Vacancy requires employer questions — skipping")
            return {"ok": False, "message": "Требуются доп. вопросы работодателя — пропускаем"}

        if resume_select or preferred_resume_title or preferred_resume_id:
            log.info(
                "Selecting resume in hh apply flow (title=%r, id=%r)",
                preferred_resume_title,
                preferred_resume_id,
            )
            selected = await select_preferred_resume()
            if not selected:
                return {"ok": False, "message": "Не удалось выбрать нужное резюме"}
            await self._page.wait_for_timeout(1000)

        if not letter_field:
            (
                _,
                _,
                _,
                _,
                letter_field,
                submit_btn,
            ) = await detect_response_controls()

        if letter_field and cover_letter:
            log.info("Filling cover letter...")
            await letter_field.scroll_into_view_if_needed()
            await self._page.wait_for_timeout(300)
            await letter_field.fill("")
            await letter_field.type(cover_letter, delay=20)
            await self._page.wait_for_timeout(500)

        if submit_btn:
            clicked = await self._click_with_fallbacks(submit_btn, "submit_button")
            if not clicked:
                return {"ok": False, "message": "Не удалось нажать кнопку подтверждения"}
            await self._page.wait_for_timeout(4000)

        await save_debug_snapshot("debug_apply_after_submit")

        # Проверяем успех
        success = await self._page.query_selector(
            "[data-qa*='responded'], "
            "button:has-text('Вы откликнулись')"
        )
        if success:
            return {"ok": True, "message": "Отклик отправлен"}

        success_notification = await self._page.query_selector(
            "[data-qa='vacancy-response-success-standard-notification'], "
            "[data-qa*='success-standard-notification']"
        )
        if success_notification:
            return {"ok": True, "message": "Отклик отправлен (success notification)"}

        # Проверяем: страница изменилась (перешли на negotiations)
        current_url = self._page.url
        if "/negotiations" in current_url:
            return {"ok": True, "message": "Отклик отправлен (negotiations page)"}

        # Проверяем: может появилось сообщение об успешном отклике
        page_text = await self._page.evaluate("() => document.body.innerText")
        page_lower = page_text.lower()
        if "отклик" in page_lower and ("отправлен" in page_lower or "откликнулись" in page_lower):
            if cover_letter:
                await self._fill_cover_letter_post_apply(cover_letter)
            return {"ok": True, "message": "Отклик отправлен (по тексту страницы)"}

        # Captcha после submit
        if await self._is_captcha_page():
            log.warning("Captcha appeared after apply submit")
            return {"ok": False, "message": "hh.ru показал captcha после отклика"}

        # Ошибка rate limit / блокировки
        if "слишком много" in page_lower or "too many" in page_lower:
            log.warning("Rate limit detected after apply")
            return {"ok": False, "message": "hh.ru rate limit — слишком много откликов"}

        # Ошибка на стороне hh
        if "что-то пошло не так" in page_lower or "ошибка" in page_lower:
            log.warning("hh.ru error page after apply. URL: %s", current_url)
            return {"ok": False, "message": "hh.ru показал ошибку после отклика"}

        log.warning("Apply verification failed. URL: %s", current_url)
        await save_debug_snapshot("debug_apply_verification_failed")
        return {"ok": False, "message": "Не удалось подтвердить отклик"}

    async def _fill_cover_letter_post_apply(self, cover_letter: str):
        """Заполнить сопроводительное письмо на странице после успешного отклика."""
        try:
            # На странице "Резюме доставлено" есть textarea для сопроводительного
            letter_field = await self._page.query_selector(
                "textarea[placeholder*='Сопроводительное'], "
                "textarea[placeholder*='сопроводительное'], "
                "textarea"
            )
            if not letter_field:
                log.debug("No cover letter field found after apply")
                return

            await letter_field.click()
            await self._page.wait_for_timeout(300)
            await letter_field.fill(cover_letter)
            await self._page.wait_for_timeout(500)

            # Ищем кнопку "Отправить"
            send_btn = await self._page.query_selector(
                "button:has-text('Отправить')"
            )
            if send_btn:
                await send_btn.scroll_into_view_if_needed()
                await self._page.wait_for_timeout(300)
                await send_btn.click()
                await self._page.wait_for_timeout(2000)
                log.info("Cover letter sent after apply")
            else:
                log.debug("No send button found for cover letter")
        except Exception as e:
            log.warning("Failed to fill cover letter post-apply: %s", e)

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
        try:
            await self._page.goto(
                f"{config.HH_BASE_URL}/applicant/resumes",
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except Exception as e:
            log.warning("Resume page navigation issue: %s", e)

        await self._page.wait_for_timeout(4000)

        # Дебаг: скриншот и URL
        current_url = self._page.url
        log.info("Resume page URL: %s", current_url)
        debug_screenshot = os.path.join(config.HH_STATE_DIR, "debug_resumes.png")
        debug_html = os.path.join(config.HH_STATE_DIR, "debug_resumes.html")
        try:
            await self._page.screenshot(path=debug_screenshot)
            html = await self._page.content()
            with open(debug_html, "w") as f:
                f.write(html)
            log.info("Debug saved: %s, %s", debug_screenshot, debug_html)
        except Exception as e:
            log.debug("Debug save failed: %s", e)

        resumes = []

        # Стратегия 1: data-qa селекторы
        cards = await self._page.query_selector_all("[data-qa='resume']")
        log.info("Strategy 1 (data-qa='resume'): %d cards", len(cards))

        # Стратегия 2: ссылки с /resume/ в href
        if not cards:
            cards = await self._page.query_selector_all("a[href*='/resume/']")
            log.info("Strategy 2 (a[href*='/resume/']): %d links", len(cards))
            seen_ids = set()
            for link in cards:
                href = await link.get_attribute("href") or ""
                if "/resume/" not in href:
                    continue
                resume_id = href.split("/resume/")[-1].split("?")[0].split("/")[0]
                if not resume_id or resume_id in seen_ids:
                    continue
                seen_ids.add(resume_id)
                title = (await link.inner_text()).strip() or resume_id
                if not title or len(title) > 200:
                    title = resume_id
                resumes.append({"id": resume_id, "title": title, "url": href})
            return resumes

        # Стратегия 1 продолжение: парсим карточки
        for card in cards:
            title_el = await card.query_selector(
                "[data-qa='resume-title'], "
                "a[data-qa*='title'], "
                "a[href*='/resume/']"
            )
            if title_el:
                title = (await title_el.inner_text()).strip()
                href = await title_el.get_attribute("href") or ""
                resume_id = ""
                if "/resume/" in href:
                    resume_id = href.split("/resume/")[-1].split("?")[0].split("/")[0]
                resumes.append({"id": resume_id, "title": title, "url": href})

        return resumes

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
