"""Клиент для поиска вакансий и отклика на GeekJob."""
import asyncio
import html
import json
import logging
import os
import re
import time

import aiohttp
from playwright.async_api import BrowserContext, Page, async_playwright

import config

log = logging.getLogger("geekjob_client")


def _clean_html(value: str | None) -> str:
    if not value:
        return ""
    text = html.unescape(value)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<hr\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>|</div>|</section>|</article>|</h\d>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"</li>|</ul>|</ol>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.S | re.I)
    return match.group(1) if match else ""


def _ensure_dirs():
    os.makedirs(os.path.dirname(config.GEEKJOB_COOKIES_FILE), exist_ok=True)
    os.makedirs(config.HH_STATE_DIR, exist_ok=True)


def _load_cookies() -> list[dict] | None:
    if os.path.exists(config.GEEKJOB_COOKIES_FILE):
        try:
            with open(config.GEEKJOB_COOKIES_FILE) as f:
                return json.load(f)
        except Exception as exc:
            log.warning("Failed to load GeekJob cookies: %s", exc)
    return None


def _save_cookies(cookies: list[dict]):
    _ensure_dirs()
    with open(config.GEEKJOB_COOKIES_FILE, "w") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)


def _cookie_header(cookies: list[dict] | None) -> str:
    if not cookies:
        return ""

    now = time.time()
    parts = []
    for cookie in cookies:
        name = (cookie.get("name") or "").strip()
        value = cookie.get("value")
        domain = (cookie.get("domain") or "").lstrip(".").lower()
        expires = cookie.get("expires")

        if not name or value is None:
            continue
        if domain and "geekjob.ru" not in domain:
            continue
        if isinstance(expires, (int, float)) and expires > 0 and expires < now:
            continue

        parts.append(f"{name}={value}")

    return "; ".join(parts)


class GeekJobClient:
    """Парсит публичный SSR-листинг и шлёт отклики через JSON API GeekJob."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._pw = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._apply_context_cache: dict[str, dict] = {}

    async def start(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"
                    ),
                    "Accept-Language": "ru,en;q=0.9",
                },
                timeout=aiohttp.ClientTimeout(total=20),
                trust_env=True,
            )

    async def stop(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        await self.stop_browser()

    async def start_browser(self, headless: bool | None = None):
        if self._page is not None:
            return

        _ensure_dirs()
        self._pw = await async_playwright().start()

        launch_opts = {
            "headless": config.HEADLESS if headless is None else headless,
            "slow_mo": config.SLOW_MO,
        }
        proxy_url = os.environ.get("GEEKJOB_PROXY") or os.environ.get("HH_PROXY") or config.BROWSER_PROXY
        if proxy_url:
            launch_opts["proxy"] = {"server": proxy_url}
            log.info("Using proxy for GeekJob browser: %s", proxy_url)

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
            log.info("Loaded %d GeekJob cookies", len(cookies))

        self._page = await self._context.new_page()

    async def stop_browser(self):
        if self._context:
            try:
                cookies = await self._context.cookies()
                _save_cookies(cookies)
            except Exception:
                pass
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._context = None
        self._browser = None
        self._pw = None
        self._page = None

    async def save_session(self):
        if self._context:
            cookies = await self._context.cookies()
            _save_cookies(cookies)
            log.info("GeekJob session saved (%d cookies)", len(cookies))

    async def _get_text(self, url: str) -> str:
        await self.start()
        assert self._session is not None
        async with self._session.get(url, ssl=False) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"GeekJob error {resp.status}: {text[:300]}")
            return await resp.text()

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict | None = None,
        referer: str | None = None,
    ) -> dict:
        await self.start()
        assert self._session is not None

        full_url = url if url.startswith("http") else f"{config.GEEKJOB_BASE_URL}{url}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        cookie_header = _cookie_header(_load_cookies())
        if cookie_header:
            headers["Cookie"] = cookie_header
        if referer:
            headers["Referer"] = referer
        if method.upper() != "GET":
            headers["Origin"] = config.GEEKJOB_BASE_URL

        async with self._session.request(
            method.upper(),
            full_url,
            headers=headers,
            json=payload,
            ssl=False,
        ) as resp:
            raw = await resp.text()
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"GeekJob returned non-JSON for {full_url}: {raw[:300]}"
                ) from exc

    def _build_list_url(self, page: int) -> str:
        if page <= 1:
            return f"{config.GEEKJOB_BASE_URL}/vacancies"
        return f"{config.GEEKJOB_BASE_URL}/vacancies/{page}"

    def _extract_total_pages(self, page_text: str) -> int:
        raw = _first_match(page_text, r"<small>\s*страниц\s+(\d+)\s*</small>")
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1

    def _extract_list_items(self, page_text: str) -> list[str]:
        serplist = _first_match(
            page_text,
            r'<ul class="collection serp-list" id="serplist">(.*?)</ul>',
        )
        if not serplist:
            return []
        return re.findall(
            r'<li class="collection-item avatar[^"]*">(.*?)</li>',
            serplist,
            re.S | re.I,
        )

    def _normalize_vacancy(self, item_html: str) -> dict | None:
        href = _first_match(item_html, r'href="(/vacancy/[^"]+)"')
        title = _clean_html(_first_match(item_html, r'<a href="[^"]+" class="title"[^>]*>(.*?)</a>'))
        company = _clean_html(
            _first_match(item_html, r'<p class="truncate company-name">\s*<a [^>]*>(.*?)</a>\s*</p>')
        )
        top_info = _first_match(
            item_html,
            r'<div class="info">\s*<a href="/vacancy/[^"]+"[^>]*>(.*?)</a>\s*</div>',
        )
        labels_html = _first_match(
            item_html,
            r'<p class="truncate company-name">.*?</p>\s*<div class="info">(.*?)</div>',
        )
        published_at = _clean_html(
            _first_match(item_html, r'<time class="truncate datetime-info">\s*<a [^>]*>(.*?)</a>')
        )

        if not href or not title:
            return None

        location_html, _, salary_html = top_info.partition("<br")
        salary = _clean_html(_first_match(top_info, r'<span class="salary">(.*?)</span>')) or "не указана"
        location = _clean_html(location_html)
        labels = [
            _clean_html(text)
            for text in re.findall(r"<span class=\"[^\"]+\">(.*?)</span>", labels_html, re.S | re.I)
            if _clean_html(text)
        ]

        snippet_parts = []
        if location:
            snippet_parts.append(f"Локация: {location}")
        if labels:
            snippet_parts.append("Формат: " + ", ".join(labels))
        if published_at:
            snippet_parts.append(f"Опубликовано: {published_at}")
        snippet = "\n".join(snippet_parts)

        external_id = href.rstrip("/").split("/")[-1]
        return {
            "id": f"geekjob:{external_id}",
            "external_id": external_id,
            "source": "geekjob",
            "source_label": "GeekJob",
            "title": title or "Без названия",
            "company": company or "—",
            "salary": salary,
            "url": f"{config.GEEKJOB_BASE_URL}{href}",
            "snippet": snippet[:1000],
            "details": snippet,
            "location": location,
            "apply_mode": "auto" if config.GEEKJOB_AUTO_APPLY else "manual",
        }

    async def search_vacancies(self, page: int = 1) -> tuple[list[dict], int]:
        page_text = await self._get_text(self._build_list_url(page))
        total_pages = self._extract_total_pages(page_text)

        normalized = []
        for item_html in self._extract_list_items(page_text):
            vacancy = self._normalize_vacancy(item_html)
            if vacancy is not None:
                normalized.append(vacancy)
        return normalized, total_pages

    async def get_vacancy_details(self, url: str) -> str:
        page_text = await self._get_text(url)

        company = _clean_html(_first_match(page_text, r'<h5 class="company-name">(.*?)</h5>'))
        location = _clean_html(_first_match(page_text, r'<div class="location">(.*?)</div>'))
        category = _clean_html(_first_match(page_text, r'<div class="category">(.*?)</div>'))
        jobinfo = _clean_html(_first_match(page_text, r'<div class="jobinfo">(.*?)</div>'))
        published_at = _clean_html(_first_match(page_text, r'<div class="time">(.*?)</div>'))
        description = _clean_html(
            _first_match(page_text, r'<div id="vacancy-description">(.*?)</div>')
        )

        tag_blocks = re.findall(r'<div class="tags">(.*?)</div>', page_text, re.S | re.I)
        tags = []
        for block in tag_blocks:
            cleaned = _clean_html(block)
            if cleaned:
                tags.append(cleaned)

        parts = []
        if company:
            parts.append(f"Компания: {company}")
        if location:
            parts.append(f"Локация: {location}")
        if category:
            parts.append(f"Уровень: {category}")
        if jobinfo:
            parts.append(f"Условия: {jobinfo}")
        if tags:
            parts.append("Теги: " + " | ".join(tags))
        if published_at:
            parts.append(f"Опубликовано: {published_at}")
        if description:
            parts.append(f"Описание:\n{description}")

        if parts:
            return "\n\n".join(parts)

        fallback = _clean_html(
            _first_match(page_text, r'<meta name="description" content="([^"]+)"')
        )
        if fallback:
            log.warning("GeekJob details fallback used for %s", url)
        return fallback

    async def login_interactive(self):
        await self.start_browser(headless=False)
        try:
            await self._page.goto(
                config.GEEKJOB_LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )
        except Exception:
            pass

        print("\n" + "=" * 60)
        print("Браузер открыт. Войди в GeekJob как специалист.")
        print("Если у тебя ещё нет резюме на GeekJob, загрузи его перед автооткликом.")
        print("После успешного входа нажми Enter здесь...")
        print("=" * 60)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, input)
        await self.save_session()
        print("✅ GeekJob cookies сохранены!")
        print(f"   Файл: {config.GEEKJOB_COOKIES_FILE}")
        await self.stop_browser()

    async def _page_is_logged_in(self) -> bool:
        signin_count = await self._page.locator("a[href*='/signin']").count()
        if signin_count == 0:
            return True

        signout_count = await self._page.locator(
            "a[href*='signout'], a[href*='logout'], a[href*='my.geekjob.ru/cv']"
        ).count()
        return signout_count > 0

    async def is_logged_in(self) -> bool:
        if self._page is None:
            await self.start_browser()

        try:
            await self._page.goto(
                config.GEEKJOB_BASE_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self._page.wait_for_timeout(1500)
            return await self._page_is_logged_in()
        except Exception as exc:
            log.warning("GeekJob login check failed: %s", exc)
            return False

    def _extract_vacancy_meta(self, page_text: str) -> dict:
        payload = _first_match(page_text, r"window\.Vacancy\s*=\s*(\{.*?\});")
        if not payload:
            raise RuntimeError("GeekJob vacancy payload not found")
        return json.loads(payload)

    def _select_resume(self, cv_list: list[dict]) -> dict | None:
        if not cv_list:
            return None

        preferred_id = config.GEEKJOB_RESUME_ID.strip()
        if preferred_id:
            for item in cv_list:
                if str(item.get("id") or "").strip() == preferred_id:
                    return item

        return cv_list[0]

    def _build_response_text(
        self,
        cover_letter: str,
        vacancy_meta: dict,
        vacancy_url: str,
        cv_item: dict | None,
        user: dict | None,
    ) -> str:
        lang = (vacancy_meta.get("lang") or "ru").lower()
        vacancy_title = vacancy_meta.get("position") or ""
        clean_url = (vacancy_url or "").split("#", 1)[0]
        user_email = ((user or {}).get("email") or "").strip()
        cv_id = str((cv_item or {}).get("id") or "").strip()
        is_public = bool((cv_item or {}).get("public", True))

        resume_hint = ""
        if cv_id:
            resume_url = f"{config.GEEKJOB_BASE_URL}/geek/{cv_id}"
            if lang == "en":
                if is_public:
                    resume_hint = f"You can see my resume here {resume_url}"
                else:
                    resume_hint = (
                        "I am looking for work anonymously. You are added to my whitelist, "
                        f"so you can see my resume at the link {resume_url}"
                    )
            else:
                if is_public:
                    resume_hint = f"Вы можете посмотреть мое резюме по ссылке {resume_url}"
                else:
                    resume_hint = (
                        'Я ищу работу анонимно. Вы добавлены в мой "белый" список, '
                        f"поэтому вы можете увидеть мое резюме по ссылке {resume_url}"
                    )
        elif user_email:
            if lang == "en":
                resume_hint = f"I have not uploaded a resume file, but you can write me on e-mail {user_email}"
            else:
                resume_hint = f"Я не загрузил резюме, но вы можете написать мне на почту {user_email}"

        cover = (cover_letter or "").strip()
        if cover:
            parts = [cover]
            if resume_hint:
                parts.append(resume_hint)
            return "\n\n".join(parts)

        if lang == "en":
            parts = [
                f'Hello! I was interested in your vacancy\n"{vacancy_title}" ({clean_url})',
                resume_hint,
                "If you are interested in me, answer, please. Have a nice day!",
            ]
        else:
            parts = [
                f'Здравствуйте!\nМеня заинтересовала ваша вакансия "{vacancy_title}" ({clean_url})',
                resume_hint,
                "Заранее благодарю за ответ.",
            ]
        return "\n\n".join(part for part in parts if part)

    async def _get_apply_context(self, vacancy_url: str, *, refresh: bool = False) -> dict:
        if not vacancy_url:
            raise RuntimeError("GeekJob vacancy URL is missing")

        if not refresh and vacancy_url in self._apply_context_cache:
            return self._apply_context_cache[vacancy_url]

        page_text = await self._get_text(vacancy_url)
        vacancy_meta = self._extract_vacancy_meta(page_text)
        mycv = await self._request_json(
            "GET",
            f"/json/mycvlist?vid={vacancy_meta['id']}",
            referer=vacancy_url,
        )
        context = {
            "vacancy": vacancy_meta,
            "mycv": mycv,
            "vacancy_url": vacancy_url,
        }
        self._apply_context_cache[vacancy_url] = context
        return context

    async def is_auto_apply_ready(self, vacancy_url: str) -> tuple[bool, str]:
        try:
            context = await self._get_apply_context(vacancy_url)
        except Exception as exc:
            return False, f"Не удалось проверить GeekJob: {exc}"

        payload = context.get("mycv") or {}
        if payload.get("error"):
            return False, payload.get("message", "GeekJob не готов к автоотклику")
        return True, "ready"

    async def apply_to_vacancy(self, vacancy: dict, cover_letter: str = "") -> dict:
        vacancy_url = vacancy.get("url") or ""
        if not vacancy_url:
            return {"ok": False, "message": "Не найден URL вакансии GeekJob"}

        try:
            context = await self._get_apply_context(vacancy_url, refresh=True)
        except Exception as exc:
            return {"ok": False, "message": f"Не удалось открыть GeekJob: {exc}"}

        vacancy_meta = context.get("vacancy") or {}
        payload = context.get("mycv") or {}

        if payload.get("error"):
            return {"ok": False, "message": payload.get("message", "GeekJob отклик недоступен")}
        if payload.get("responded"):
            return {"ok": True, "message": "Уже откликались ранее"}

        cv_list = payload.get("data") or []
        user = payload.get("user") or {}
        selected_cv = self._select_resume(cv_list)
        cvid = (selected_cv or {}).get("id")
        text = self._build_response_text(cover_letter, vacancy_meta, vacancy_url, selected_cv, user)

        response = await self._request_json(
            "POST",
            "/json/respond/vacancy",
            payload={
                "text": text,
                "vic": vacancy_meta.get("ic"),
                "vid": vacancy_meta.get("id"),
                "vci": vacancy_meta.get("ci"),
                "cid": cvid,
            },
            referer=vacancy_url,
        )

        if response.get("error"):
            return {"ok": False, "message": response.get("message", "Не удалось отправить отклик")}

        self._apply_context_cache.pop(vacancy_url, None)
        return {
            "ok": True,
            "message": response.get("message", "Отклик отправлен"),
            "resume_id": cvid,
        }
