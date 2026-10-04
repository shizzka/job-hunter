"""Клиент для поиска вакансий и отклика на GeekJob."""
import asyncio
import copy
import hashlib
import html
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit

import aiohttp
from playwright.async_api import BrowserContext, Page, async_playwright

import config
import proxy_utils
from browser_cookie_session import BrowserCookieSession
from state_store.browser_cookies import CookieRepository
from state_store.geekjob_apply import GeekJobApplyRepository, canonical_vacancy_url

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
    return CookieRepository(config.GEEKJOB_COOKIES_FILE).snapshot()[0]


def _save_cookies(cookies: list[dict]):
    _ensure_dirs()
    CookieRepository(config.GEEKJOB_COOKIES_FILE).save(cookies)


def _cookie_header(cookies: list[dict] | None, *, url='https://geekjob.ru/json/') -> str:
    if not cookies:
        return ""

    now = time.time()
    parts = {}
    target = urlsplit(url)
    host, request_path = (target.hostname or '').casefold(), target.path or '/'
    for cookie in cookies:
        name = (cookie.get("name") or "").strip()
        value = cookie.get("value")
        raw_domain = (cookie.get('domain') or '').casefold()
        domain = raw_domain.lstrip('.')
        expires = cookie.get("expires")

        if not name or value is None:
            continue
        if domain and not (host == domain or (raw_domain.startswith('.') and host.endswith('.' + domain))):
            continue
        cookie_path = cookie.get('path') or '/'
        if not (request_path == cookie_path or (request_path.startswith(cookie_path)
                and (cookie_path.endswith('/') or request_path[len(cookie_path):].startswith('/')))):
            continue
        if cookie.get('secure') and target.scheme != 'https':
            continue
        if isinstance(expires, (int, float)) and expires > 0 and expires < now:
            continue

        if name in parts and parts[name] != value:
            raise RuntimeError('Ambiguous GeekJob API cookie identity')
        parts[name] = value

    return '; '.join(f'{name}={value}' for name, value in sorted(parts.items()))


class GeekJobClient:
    """Парсит публичный SSR-листинг и шлёт отклики через JSON API GeekJob."""

    def __init__(self):
        self._cookie_session = BrowserCookieSession(config.GEEKJOB_COOKIES_FILE, config.HH_STATE_DIR)
        self._base_url = config.GEEKJOB_BASE_URL.rstrip('/')
        self._resume_id = config.GEEKJOB_RESUME_ID.strip()
        self._browser_settings = (config.HEADLESS, config.SLOW_MO, config.BROWSER_PROXY)
        self._api_cookies = None
        self._api_revision = None
        self._api_loaded = False
        self._api_revoked = False
        self._api_account = ''
        self._apply_repository = GeekJobApplyRepository(self._cookie_session.repository.path)
        self._session: aiohttp.ClientSession | None = None
        self._session_uses_env_proxy = True
        self._pw = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._apply_context_cache: dict[str, dict] = {}

    def _api_cookie_snapshot(self):
        if self._api_revoked:
            raise RuntimeError('GeekJob account/session superseded; restart client')
        cookies, revision = self._cookie_session.repository.snapshot()
        if self._api_loaded and revision != self._api_revision:
            self._api_revoked = True
            self._apply_context_cache.clear()
            raise RuntimeError('GeekJob account/session changed; restart client')
        if not self._api_loaded:
            self._api_cookies, self._api_revision, self._api_loaded = copy.deepcopy(cookies), revision, True
        return copy.deepcopy(self._api_cookies), self._api_revision

    def _session_identity(self):
        cookies, revision = self._api_cookie_snapshot()
        submit_header = _cookie_header(cookies, url=self._base_url + '/json/respond/vacancy')
        account_header = _cookie_header(cookies, url=self._base_url + '/json/mycvlist')
        if not submit_header:
            raise RuntimeError('GeekJob authenticated cookie session unavailable')
        # Source cookie names are undocumented: conservatively refuse different
        # effective credentials rather than use GET account A to POST as B.
        if account_header != submit_header:
            raise RuntimeError('GeekJob GET/POST effective account cookies differ')
        return hashlib.sha256(repr(revision).encode()).hexdigest()

    def _bind_account(self, payload):
        user = payload.get('user')
        if not isinstance(user, dict):
            raise RuntimeError('GeekJob account identity unavailable')
        user_id, email = user.get('id'), user.get('email')
        if user_id is not None and type(user_id) not in (str, int):
            raise RuntimeError('GeekJob invalid account identity')
        if email is not None and not isinstance(email, str):
            raise RuntimeError('GeekJob invalid account identity')
        identity = str(user_id or '').strip() or str(email or '').strip().casefold()
        if not identity:
            raise RuntimeError('GeekJob account identity unavailable')
        account = hashlib.sha256(identity.encode()).hexdigest()
        if self._api_account and self._api_account != account:
            self._api_revoked = True
            raise RuntimeError('GeekJob API account changed')
        self._api_account = account
        return account

    async def _verify_browser_api_owner(self):
        cookies, revision = self._api_cookie_snapshot()
        if self._context is None:
            if self._page is not None:
                raise RuntimeError('GeekJob browser ownership unavailable')
            return
        context, binding = self._context, self._cookie_session.binding
        if (binding is None or binding.context is not context or binding.closing
                or binding.revoked or binding.revision != revision):
            raise RuntimeError('GeekJob browser/API cookie owner mismatch')
        captured = await context.cookies()
        if (context is not self._context or binding is not self._cookie_session.binding
                or binding.closing or binding.revoked or binding.revision != revision
                or self._api_cookie_snapshot()[1] != revision
                or _cookie_header(captured, url=self._base_url + '/json/respond/vacancy') !=
                   _cookie_header(cookies, url=self._base_url + '/json/respond/vacancy')):
            raise RuntimeError('GeekJob browser/API account changed')

    async def start(self, *, trust_env: bool = True):
        if self._session and not self._session.closed:
            if self._session_uses_env_proxy == trust_env:
                return
            await self._session.close()
            self._session = None

        self._session = aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(),  # GET Set-Cookie must not replace frozen API credentials.
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                "Accept-Language": "ru,en;q=0.9",
            },
            timeout=aiohttp.ClientTimeout(total=20),
            trust_env=trust_env,
        )
        self._session_uses_env_proxy = trust_env

    async def stop(self):
        session = self._session
        binding = self._cookie_session.binding
        try:
            if session and not session.closed:
                await session.close()
        finally:
            if self._session is session:
                self._session = None
            if self._cookie_session.binding is binding:
                await self.stop_browser()

    async def start_browser(self, headless: bool | None = None):
        launch_opts = {
            "headless": self._browser_settings[0] if headless is None else headless,
            "slow_mo": self._browser_settings[1],
        }
        proxy_url = os.environ.get("GEEKJOB_PROXY") or os.environ.get("HH_PROXY") or self._browser_settings[2]
        if proxy_url:
            launch_opts["proxy"] = {"server": proxy_url}
            log.info("Using configured proxy for GeekJob browser")
        launch_opts["env"] = proxy_utils.browser_launch_env(proxy_url)

        await self._cookie_session.start(self, playwright_factory=async_playwright,
                                         launch_options=launch_opts, logger=log)

    async def stop_browser(self):
        await self._cookie_session.stop(self, logger=log)

    async def save_session(self):
        await self._cookie_session.save(self, logger=log)

    async def _get_text_once(self, url: str) -> str:
        assert self._session is not None
        self._validate_origin(url)
        async with self._session.get(url, ssl=True, allow_redirects=False) as resp:
            if resp.status != 200:
                raise RuntimeError(f'GeekJob unsuccessful public response ({resp.status})')
            return await resp.text()

    async def _get_text(self, url: str) -> str:
        await self.start()
        try:
            return await self._get_text_once(url)
        except Exception as exc:
            if self._session_uses_env_proxy and proxy_utils.is_proxy_error(exc):
                log.warning("GeekJob proxy failed, retrying direct: %s", exc)
                await self.start(trust_env=False)
                return await self._get_text_once(url)
            raise

    async def _request_json_once(
        self,
        method: str,
        url: str,
        *,
        payload: dict | None = None,
        referer: str | None = None,
        before_send=None,
    ) -> dict:
        assert self._session is not None

        full_url = url if url.startswith("http") else f"{self._base_url}{url}"
        self._validate_origin(full_url)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        cookie_header = _cookie_header(self._api_cookie_snapshot()[0], url=full_url)
        if cookie_header:
            headers["Cookie"] = cookie_header
        if referer:
            headers["Referer"] = referer
        if method.upper() != "GET":
            headers["Origin"] = self._base_url
        if before_send is not None:
            before_send()

        async with self._session.request(
            method.upper(),
            full_url,
            headers=headers,
            json=payload,
            ssl=True,
            allow_redirects=False,
        ) as resp:
            raw = await resp.text()
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError('GeekJob returned malformed JSON') from exc
            if resp.status != 200 or not isinstance(value, dict):
                raise RuntimeError('GeekJob unsuccessful API response')
            self._api_cookie_snapshot()  # Never adopt cookies changed during GET/POST.
            return value

    def _validate_origin(self, url):
        target, origin = urlsplit(url), urlsplit(self._base_url)
        if (target.scheme != 'https' or target.username is not None or target.password is not None
                or (target.hostname, target.port or 443) != (origin.hostname, origin.port or 443)):
            raise RuntimeError('GeekJob authenticated API origin mismatch')

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict | None = None,
        referer: str | None = None,
    ) -> dict:
        await self.start()
        try:
            return await self._request_json_once(
                method,
                url,
                payload=payload,
                referer=referer,
            )
        except Exception as exc:
            if method.upper() == 'GET' and self._session_uses_env_proxy and proxy_utils.is_proxy_error(exc):
                log.warning("GeekJob GET proxy failed, retrying direct (%s)", type(exc).__name__)
                await self.start(trust_env=False)
                return await self._request_json_once(
                    method,
                    url,
                    payload=payload,
                    referer=referer,
                )
            raise

    def _build_list_url(self, page: int) -> str:
        if page <= 1:
            return f"{self._base_url}/vacancies"
        return f"{self._base_url}/vacancies/{page}"

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
            "url": f"{self._base_url}{href}",
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
            try:
                await self._page.goto(
                    config.GEEKJOB_LOGIN_URL, wait_until="domcontentloaded", timeout=60000,
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
            print(f"   Файл: {self._cookie_session.repository.path}")
        finally:
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
                self._base_url,
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
        if not isinstance(cv_list, list) or not cv_list or any(not isinstance(item, dict)
                or type(item.get('id')) not in (str, int)
                or not re.fullmatch(r'[A-Za-z0-9_-]+', str(item['id']))
                or (type(item['id']) is int and item['id'] <= 0) for item in cv_list):
            return None

        preferred_id = self._resume_id
        if preferred_id:
            matches = [item for item in cv_list if isinstance(item, dict) and str(item.get('id') or '').strip() == preferred_id]
            return matches[0] if len(matches) == 1 else None
        return cv_list[0] if len(cv_list) == 1 and isinstance(cv_list[0], dict) else None

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
            resume_url = f"{self._base_url}/geek/{cv_id}"
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
        vacancy_url = canonical_vacancy_url(vacancy_url)
        if urlsplit(vacancy_url).hostname != urlsplit(self._base_url).hostname:
            raise RuntimeError('GeekJob vacancy origin mismatch')

        session = self._session_identity()
        key = (vacancy_url, session, self._api_account)
        if not refresh and key in self._apply_context_cache:
            return copy.deepcopy(self._apply_context_cache[key])

        page_text = await self._get_text(vacancy_url)
        vacancy_meta = self._extract_vacancy_meta(page_text)
        if str(vacancy_meta.get('id') or '') != vacancy_url.rsplit('/', 1)[-1]:
            raise RuntimeError('GeekJob URL/vacancy identity mismatch')
        mycv = await self._request_json(
            "GET",
            f"/json/mycvlist?vid={vacancy_meta['id']}",
            referer=vacancy_url,
        )
        self._api_cookie_snapshot()
        if (type(mycv.get('error')) is not bool or
                (not mycv['error'] and (type(mycv.get('responded')) is not bool or not isinstance(mycv.get('data'), list)))):
            raise RuntimeError('GeekJob invalid account/resume response schema')
        if not mycv.get('error'):
            self._bind_account(mycv)
        context = {
            "vacancy": vacancy_meta,
            "mycv": mycv,
            "vacancy_url": vacancy_url,
            'session': session, 'account': self._api_account,
        }
        self._apply_context_cache[(vacancy_url, session, self._api_account)] = copy.deepcopy(context)
        return copy.deepcopy(context)

    async def is_auto_apply_ready(self, vacancy_url: str) -> tuple[bool, str]:
        try:
            context = await self._get_apply_context(vacancy_url)
        except Exception as exc:
            return False, f"Не удалось проверить сессию GeekJob ({type(exc).__name__}); нужна ручная проверка"

        payload = context.get("mycv") or {}
        if payload.get("error"):
            return False, 'GeekJob сессия не подтверждена; нужна ручная проверка'
        if not self._select_resume(payload.get('data') or []):
            return False, 'Целевое GeekJob-резюме отсутствует или неоднозначно'
        return True, "ready"

    async def apply_to_vacancy(self, vacancy: dict, cover_letter: str = "") -> dict:
        original = copy.deepcopy(vacancy)
        vacancy_url = original.get("url") or ""
        if not vacancy_url:
            return {"ok": False, "message": "Не найден URL вакансии GeekJob"}

        owner, acting = None, False
        account, cvid = '', ''
        def finish(status):
            self._apply_repository.transition(vacancy_url, owner, status, account=account, resume_id=str(cvid or ''))
        try:
            vacancy_url = canonical_vacancy_url(vacancy_url)
            expected_id = vacancy_url.rsplit('/', 1)[-1]
            provided_id = str(original.get('external_id') or original.get('id') or '').removeprefix('geekjob:')
            if provided_id != expected_id:
                raise RuntimeError('GeekJob approved vacancy identity mismatch')
            session = self._session_identity()
            approval = hashlib.sha256(json.dumps({'vacancy': original, 'cover': cover_letter,
                'resume_id': self._resume_id}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            owner = self._apply_repository.claim(vacancy_url, session, approval)
            if owner is None:
                return {'ok': False, 'submission_status': self._apply_repository.get(vacancy_url).get('status'),
                        'message': 'GeekJob отклик уже завершён или требует ручной проверки; автоматического повтора нет'}
            context = await self._get_apply_context(vacancy_url, refresh=True)
            vacancy_meta, payload = context.get('vacancy') or {}, context.get('mycv') or {}
            if payload.get('error'):
                finish('failed')
                return {'ok': False, 'message': 'GeekJob отклик недоступен; проверьте сессию вручную'}
            account = self._bind_account(payload)
            if payload.get('responded'):
                finish('completed')
                return {'ok': True, 'already_applied': True, 'message': 'Уже откликались ранее'}
            selected_cv = self._select_resume(payload.get('data') or [])
            if selected_cv is None:
                finish('failed')
                return {'ok': False, 'message': 'Целевое GeekJob-резюме отсутствует или неоднозначно'}
            cvid = selected_cv.get('id')
            text = self._build_response_text(cover_letter, vacancy_meta, vacancy_url, selected_cv, payload.get('user'))
            await self.start()
            transport = self._session
            await self._verify_browser_api_owner()
            if (vacancy != original or self._session_identity() != session
                    or context['account'] != account or context['session'] != session):
                raise RuntimeError('GeekJob approval/session changed before POST')
            finish('acting')  # Durable boundary before any possible HTTP delivery.
            acting = True
            browser_context, browser_binding = self._context, self._cookie_session.binding
            browser_nonce = self._cookie_session.nonce
            def before_send():
                record = self._apply_repository.get(vacancy_url)
                if (self._session is not transport or self._context is not browser_context
                        or self._cookie_session.binding is not browser_binding or self._cookie_session.nonce is not browser_nonce
                        or (browser_binding is not None and (browser_binding.closing or browser_binding.revoked))
                        or vacancy != original or self._session_identity() != session
                        or record.get('owner') != owner or record.get('status') != 'acting'
                        or record.get('session') != session or record.get('approval') != approval):
                    raise RuntimeError('GeekJob last-dispatch ownership/approval changed')
            response = await self._request_json_once('POST', '/json/respond/vacancy',
                payload={'text': text, 'vic': vacancy_meta.get('ic'), 'vid': vacancy_meta.get('id'),
                         'vci': vacancy_meta.get('ci'), 'cid': cvid}, referer=vacancy_url, before_send=before_send)
            if type(response.get('error')) is not bool:
                raise RuntimeError('GeekJob submit outcome unknown')
            finish('failed' if response['error'] else 'completed')
            self._apply_context_cache.clear()
            return {'ok': not response['error'], 'message': 'GeekJob отклик не принят' if response['error'] else 'Отклик отправлен',
                    'resume_id': cvid, 'resume_selection_verified': True,
                    'submission_status': 'failed' if response['error'] else 'completed'}
        except BaseException as exc:
            status = 'uncertain' if acting else 'failed'
            if owner is not None:
                try:
                    finish(status)
                except Exception as persistence_error:
                    log.warning('GeekJob submit completion unavailable (%s)', type(persistence_error).__name__)
            if not isinstance(exc, Exception):
                raise
            return {'ok': False, 'submission_status': status, 'error_kind': type(exc).__name__,
                    'message': 'GeekJob результат требует ручной проверки; автоматического повтора нет' if acting else
                               'GeekJob сессия/резюме/approval не проверены; отклик не отправлен'}
