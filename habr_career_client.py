"""Клиент для поиска вакансий на Хабр Карьере."""
import asyncio
import html
import json
import logging
import os
import re
import uuid

import aiohttp
from playwright.async_api import async_playwright, BrowserContext, Page

import config
import proxy_utils
from browser_cookie_session import BrowserCookieSession
from state_store.browser_cookies import CookieRepository
from state_store.native_apply import NativeApplyRepository, run_native_attempt

log = logging.getLogger("habr_career_client")


def _clean_html(value: str | None) -> str:
    if not value:
        return ""
    text = html.unescape(value)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>|</li>|</ul>|</ol>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _find_value(payload, key: str):
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = _find_value(value, key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_value(item, key)
            if found is not None:
                return found
    return None


def _extract_json_block(page: str, pattern: str):
    match = re.search(pattern, page, re.S)
    if not match:
        raise RuntimeError("Habr Career page JSON payload not found")
    return json.loads(match.group(1))


def _ensure_dirs():
    os.makedirs(os.path.dirname(config.HABR_COOKIES_FILE), exist_ok=True)
    os.makedirs(config.HH_STATE_DIR, exist_ok=True)


def _load_cookies() -> list[dict] | None:
    return CookieRepository(config.HABR_COOKIES_FILE).snapshot()[0]


def _save_cookies(cookies: list[dict]):
    _ensure_dirs()
    CookieRepository(config.HABR_COOKIES_FILE).save(cookies)


class HabrCareerClient:
    """Парсинг публичных страниц Хабр Карьеры без браузера."""

    def __init__(self):
        self._cookie_session = BrowserCookieSession(config.HABR_COOKIES_FILE, config.HH_STATE_DIR)
        self._session: aiohttp.ClientSession | None = None
        self._session_uses_env_proxy = True
        self._pw = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def start(self, *, trust_env: bool = True):
        if self._session and not self._session.closed:
            if self._session_uses_env_proxy == trust_env:
                return
            await self._session.close()
            self._session = None

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
            "headless": config.HEADLESS if headless is None else headless,
            "slow_mo": config.SLOW_MO,
        }
        proxy_url = (
            os.environ.get("HABR_PROXY")
            or os.environ.get("HH_PROXY")
            or config.BROWSER_PROXY
        )
        if proxy_url:
            launch_opts["proxy"] = {"server": proxy_url}
            log.info("Using configured proxy for Habr Career browser")
        launch_opts["env"] = proxy_utils.browser_launch_env(proxy_url)

        await self._cookie_session.start(self, playwright_factory=async_playwright,
                                         launch_options=launch_opts, logger=log)

    async def stop_browser(self):
        await self._cookie_session.stop(self, logger=log)

    async def save_session(self):
        await self._cookie_session.save(self, logger=log)

    async def _page_is_logged_in(self) -> bool:
        try:
            result = await self._page.evaluate(
                "() => Boolean(window.app && window.app.isUserLoggedIn)"
            )
            if isinstance(result, bool):
                return result
        except Exception:
            pass

        login_link = await self._page.query_selector("a[href*='/users/auth/tmid']")
        return login_link is None

    def _destination_matches(self, expected):
        try:
            repository = NativeApplyRepository(self._cookie_session.repository.path, 'habr')
            return repository.key(self._page.url) == repository.key(expected)
        except (ValueError, TypeError, AttributeError):
            return False

    async def _arm_destination_boundary(self, control, expected):
        self._destination_boundary_id = uuid.uuid4().hex
        return await control.evaluate(r"""(control,expected) => {
            /* codex:native-destination-arm */
            const identity = value => {
                try {
                    const url=new URL(value),host=url.hostname.toLowerCase();
                    const hostOk=expected.source==='habr' ? host==='career.habr.com' : host==='superjob.ru'||host.endsWith('.superjob.ru');
                    const match=url.pathname.match(expected.source==='habr' ? /^\/vacancies\/(\d+)\/?$/ : /^\/vakansii\/(?:[^/]*-)?(\d+)\.html\/?$/);
                    return url.protocol==='https:' && !url.username && !url.password && (!url.port||url.port==='443') && hostOk && match ? match[1] : null;
                } catch {return null;}
            };
            const id=identity(expected.url);
            if (!id || identity(location.href)!==id || !control.isConnected) return false;
            const root=control.form || control.parentElement?.closest('[data-vacancy-id],form,main,article,section') || control.parentElement;
            if (!root) return false;
            const fields=()=>[...new Set([...root.querySelectorAll('input,textarea,select,[contenteditable="true"]'),
                ...(root.tagName==='FORM'?[...root.elements].filter(el=>el.matches('input,textarea,select')):[])])];
            const entries=submitter=>root.tagName==='FORM'?[...new FormData(root,submitter?.form===root&&submitter.type==='submit'?submitter:undefined).entries()]:[];
            const payload=submitter=>JSON.stringify(entries(submitter).map(([key,value])=>[key,typeof value==='string'?value:[value.name,value.size,value.type]]));
            const contextMatches=(submitter=null)=> {
                const ids=[root.getAttribute('data-vacancy-id'),...fields().filter(el=>['vacancy_id','vacancyId','vacancy'].includes(el.name))].map(value=>typeof value==='string'||value===null?value:value.value).filter(Boolean);
                return ids.every(value=>value===id) && entries(submitter).filter(([key])=>['vacancy_id','vacancyId','vacancy'].includes(key)).every(([,value])=>value===id);
            };
            if (!contextMatches(control)) return false;
            const snapshot=()=>JSON.stringify([[...root.attributes].map(a=>[a.name,a.value]),
                [...document.querySelectorAll('[data-vacancy-id]')].map(el=>el.getAttribute('data-vacancy-id')),
                [...document.querySelectorAll('h1')].map(el=>el.textContent),
                [...root.querySelectorAll('a[href]')].map(el=>el.getAttribute('href')),
                fields().map(el=>[el.tagName,el.type,el.name,el.getAttribute('form'),el.value??el.textContent,el.checked,el.disabled]),payload(control),
                control.innerText,control.name,control.value,control.type,control.getAttribute('href'),control.getAttribute('formaction')]);
            const originalFields=fields(),approved=snapshot(),url=location.href;
            const valid=()=>root.isConnected && control.isConnected && (root.contains(control)||control.form===root) && location.href===url &&
                identity(location.href)===id && contextMatches(control) && fields().length===originalFields.length &&
                fields().every((el,i)=>el===originalFields[i]) && snapshot()===approved;
            document.__nativeDestinationApproval={id:expected.boundary_id,root,control,valid,source:expected.source,blocked:false};
            if (!document.__nativeDestinationBoundary) {
                document.__nativeDestinationBoundary=true;
                for (const name of ['click','submit']) document.addEventListener(name,event=>{
                    const approval=document.__nativeDestinationApproval;
                    if (!approval) return;
                    const target=event.target.closest?.('button,a,[role="button"],input[type="submit"]');
                    const candidate=target===approval.control || (approval.source==='superjob' ?
                        target?.matches('button.f-test-vacancy-response-button,button.f-test-button-Otkliknutsya[type="submit"]') :
                        /^(откликнуться|отправить)/i.test(target?.innerText?.trim()||''));
                    if (name==='click' && !candidate) return;
                    if ((name==='click' ? target===approval.control : event.target===approval.root) && approval.valid()) return;
                    approval.blocked=true;event.preventDefault();event.stopImmediatePropagation();
                },true);
            }
            return true;
        }""", {'url':expected,'source':'habr','boundary_id':self._destination_boundary_id}) is True

    async def _destination_boundary_passed(self):
        return await self._page.evaluate('/* codex:native-destination-readback */ id => document.__nativeDestinationApproval?.id === id && !document.__nativeDestinationApproval.blocked', self._destination_boundary_id) is True

    async def _click_with_fallbacks(self, element, label: str) -> bool:
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

        strategies = strategies[:1]  # A click error may follow delivery; never replay.
        for strategy_name, action in strategies:
            try:
                log.info("Clicking %s via %s strategy", label, strategy_name)
                expected = getattr(self, '_apply_destination', '')
                if expected and not self._destination_matches(expected):
                    raise RuntimeError('Habr destination changed before action')
                if expected and not await self._arm_destination_boundary(element, expected):
                    return False
                attempt = getattr(self, "_external_attempt", None)
                if "submit" in label and attempt is not None:
                    attempt.begin()
                await action()
                if expected and not await self._destination_boundary_passed():
                    return False
            except Exception as exc:
                log.warning("%s click via %s failed: %s", label, strategy_name, exc)
                raise
            await self._page.wait_for_timeout(1000)
            return True

        return False

    async def login_interactive(self):
        await self.start_browser(headless=False)
        try:
            try:
                await self._page.goto(
                    config.HABR_LOGIN_URL, wait_until="domcontentloaded", timeout=60000,
                )
            except Exception:
                pass
            print("\n" + "=" * 60)
            print("Браузер открыт. Залогинься на Хабр Карьере.")
            print("После успешного входа нажми Enter здесь...")
            print("=" * 60)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, input)
            await self.save_session()
            print("✅ Habr cookies сохранены!")
        finally:
            await self.stop_browser()

    async def is_logged_in(self) -> bool:
        if self._page is None:
            await self.start_browser()
        try:
            await self._page.goto(
                config.HABR_CAREER_BASE_URL,
                wait_until="domcontentloaded",
                timeout=20000,
            )
            await self._page.wait_for_timeout(1500)
            return await self._page_is_logged_in()
        except Exception as exc:
            log.warning("Habr login check failed: %s", exc)
        return False

    async def apply_to_vacancy(self, vacancy_url: str, cover_letter: str = "") -> dict:
        repository = NativeApplyRepository(self._cookie_session.repository.path, "habr")
        return await run_native_attempt(self, repository, vacancy_url,
                                        lambda: self._apply_to_vacancy(vacancy_url, cover_letter))

    async def _apply_to_vacancy(self, vacancy_url: str, cover_letter: str = "") -> dict:
        self._apply_destination = vacancy_url
        if self._page is None:
            await self.start_browser()

        try:
            await self._page.goto(vacancy_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            log.warning("Habr vacancy navigation failed: %s", type(exc).__name__)
            return {'ok': False, 'reason': 'destination_unverified', 'message': 'Навигация Habr не подтверждена'}

        await self._page.wait_for_timeout(2500)
        if not self._destination_matches(vacancy_url):
            return {'ok': False, 'reason': 'destination_unverified', 'message': 'Открыта другая вакансия Habr'}

        if not await self._page_is_logged_in():
            return {"ok": False, "message": "Не залогинен на Хабр Карьере"}

        try:
            from private_artifacts import capture_artifacts
            await capture_artifacts(self._page, self._cookie_session.state_dir, "debug_habr_apply_page", html=False)
        except Exception:
            pass

        apply_btn = await self._page.query_selector(
            "button:has-text('Откликнуться'), "
            "a:has-text('Откликнуться'), "
            "button:has-text('Откликнуться на вакансию')"
        )

        if not apply_btn:
            already = await self._page.query_selector(
                "button:has-text('Вы откликнулись'), "
                "button:has-text('Отклик отправлен')"
            )
            if not already:
                already = await self._page.query_selector("text=Вы откликнулись")
            if not already:
                already = await self._page.query_selector("text=Отклик отправлен")
            if already:
                return {"ok": True, "message": "Уже откликались ранее"}
            return {"ok": False, "message": "Кнопка отклика на Хабр Карьере не найдена"}

        letter_field = await self._page.query_selector("textarea, [contenteditable='true']")
        if not letter_field:
            # The first apply control can itself send a quick response. Treat it
            # as external, even when its result happens to open another form.
            attempt = getattr(self, "_external_attempt", None)
            if attempt is not None:
                attempt.begin()
            clicked = await self._click_with_fallbacks(apply_btn, "habr_apply_button")
            if not clicked:
                return {"ok": False, "message": "Не удалось нажать кнопку отклика"}
            await self._page.wait_for_timeout(1500)
            letter_field = await self._page.query_selector("textarea, [contenteditable='true']")
            if letter_field:
                return {"ok": False, "uncertain": True,
                        "message": "После первого действия нужна ручная сверка; второй submit не выполняется"}
        if letter_field and cover_letter:
            try:
                await letter_field.fill("")
            except Exception:
                await letter_field.evaluate("el => el.innerHTML = ''")
            try:
                await letter_field.type(cover_letter, delay=20)
            except Exception:
                await letter_field.fill(cover_letter)
            await self._page.wait_for_timeout(500)

            submit_btn = await self._page.query_selector(
                "button:has-text('Отправить'), "
                "button:has-text('Откликнуться'), "
                "button:has-text('Отправить отклик')"
            )
            if submit_btn:
                try:
                    async with self._page.expect_response(
                        lambda resp: "quick_responses" in resp.url or "/responses" in resp.url,
                        timeout=8000,
                    ) as response_info:
                        if not await self._click_with_fallbacks(submit_btn, "habr_submit_button"):
                            return {"ok": False, "message": "Не удалось подтвердить отклик"}
                    response = await response_info.value
                    if response.status < 400:
                        return {"ok": True, "message": "Отклик отправлен"}
                except Exception:
                    return {"ok": False, "message": "Результат отправки не подтверждён", "uncertain": True}

        try:
            success = await self._page.query_selector(
                "button:has-text('Вы откликнулись'), "
                "button:has-text('Отклик отправлен')"
            )
            if not success:
                success = await self._page.query_selector("text=Вы откликнулись")
            if not success:
                success = await self._page.query_selector("text=Отклик отправлен")
            if success:
                return {"ok": True, "message": "Отклик отправлен"}
        except Exception:
            pass

        return {"ok": False, "message": "Результат отправки не подтверждён", "uncertain": True}

    async def _get_text_once(self, url: str) -> str:
        assert self._session is not None
        async with self._session.get(url) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Habr Career error {resp.status}: {text[:300]}")
            return await resp.text()

    async def _get_text(self, url: str) -> str:
        await self.start()
        try:
            return await self._get_text_once(url)
        except Exception as exc:
            if self._session_uses_env_proxy and proxy_utils.is_proxy_error(exc):
                log.warning("Habr Career proxy failed, retrying direct: %s", exc)
                await self.start(trust_env=False)
                return await self._get_text_once(url)
            raise

    def _extract_ssr_state(self, page: str) -> dict:
        return _extract_json_block(
            page,
            r'<script type="application/json" data-ssr-state="true">(.*?)</script>',
        )

    async def search_vacancies(
        self,
        path: str,
        page: int = 1,
    ) -> tuple[list[dict], int]:
        query = f"?page={page}" if page > 1 else ""
        page_text = await self._get_text(f"{config.HABR_CAREER_BASE_URL}{path}{query}")
        state = self._extract_ssr_state(page_text)

        vacancies_payload = _find_value(state, "vacancies") or {}
        if isinstance(vacancies_payload, dict):
            vacancies = vacancies_payload.get("list") or []
            meta = vacancies_payload.get("meta") or {}
        else:
            vacancies = vacancies_payload if isinstance(vacancies_payload, list) else []
            meta = _find_value(state, "meta") or {}

        if not isinstance(vacancies, list):
            raise RuntimeError("Habr Career vacancies payload not found in SSR state")

        normalized = [self._normalize_vacancy(item) for item in vacancies]
        total_pages = int(meta.get("totalPages") or page or 1)
        return normalized, total_pages

    async def get_vacancy_details(self, url: str) -> str:
        page_text = await self._get_text(url)
        try:
            state = self._extract_ssr_state(page_text)
            vacancy = _find_value(state, "vacancy") or {}
            description = vacancy.get("description") or ""
            return _clean_html(description)
        except Exception as exc:
            log.warning("Failed to parse Habr Career SSR details for %s: %s", url, exc)

        ld_json = _extract_json_block(
            page_text,
            r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>',
        )
        return _clean_html(ld_json.get("description") or "")

    def _normalize_vacancy(self, item: dict) -> dict:
        salary = item.get("salary") or {}
        company = item.get("company") or {}
        locations = item.get("locations") or []
        divisions = item.get("divisions") or []
        skills = item.get("skills") or []
        qualification = item.get("salaryQualification") or {}
        snippet_parts = []
        if divisions:
            snippet_parts.append(
                "Специализации: " + ", ".join(d.get("title", "") for d in divisions if d.get("title"))
            )
        if skills:
            snippet_parts.append(
                "Навыки: " + ", ".join(s.get("title", "") for s in skills if s.get("title"))
            )
        if qualification.get("title"):
            snippet_parts.append(f"Квалификация: {qualification['title']}")
        details = "\n\n".join(snippet_parts)

        location_titles = [loc.get("title", "") for loc in locations if loc.get("title")]
        if item.get("remoteWork"):
            location_titles.insert(0, "Удаленно")

        href = item.get("href") or ""
        url = f"{config.HABR_CAREER_BASE_URL}{href}" if href.startswith("/") else href

        return {
            "id": f"habr:{item.get('id')}",
            "external_id": str(item.get("id") or ""),
            "source": "habr",
            "source_label": "Хабр Карьера",
            "title": item.get("title") or "Без названия",
            "company": company.get("title") or "—",
            "salary": salary.get("formatted") or "не указана",
            "url": url,
            "snippet": details[:1000],
            "details": details,
            "location": ", ".join(part for part in location_titles if part),
            "apply_mode": "manual",
            "published_at": (
                item["publishedDate"].get("date")
                if isinstance(item.get("publishedDate"), dict)
                else item.get("publishedDate")
            ),
        }
