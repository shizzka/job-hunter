"""Клиент для поиска вакансий и автоотклика через SuperJob API."""
import asyncio
import getpass
import html
import json
import logging
import os
import re
import uuid
from urllib.parse import urlsplit
import time
from datetime import UTC, datetime

import aiohttp
from playwright.async_api import async_playwright, BrowserContext, Page

from browser_action_boundary import RUNTIME, install_boundary, dispatch_approved, release_boundary

import config
import proxy_utils
from browser_cookie_session import BrowserCookieSession
from state_store.browser_cookies import CookieRepository
from state_store.superjob_auth import SuperJobAuthRepository, SuperJobAuthSession, SuperJobAuthError

log = logging.getLogger("superjob_client")


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _currency_symbol(code: str | None) -> str:
    return {
        "rub": "RUB",
        "uah": "UAH",
        "uzs": "UZS",
    }.get((code or "").lower(), (code or "").upper())


def _format_salary(item: dict) -> str:
    payment_from = int(item.get("payment_from") or 0)
    payment_to = int(item.get("payment_to") or 0)
    agreement = bool(item.get("agreement"))
    currency = _currency_symbol(item.get("currency"))

    if agreement and not payment_from and not payment_to:
        return "по договоренности"
    if payment_from and payment_to:
        return f"{payment_from:,}-{payment_to:,} {currency}".replace(",", " ")
    if payment_from:
        return f"от {payment_from:,} {currency}".replace(",", " ")
    if payment_to:
        return f"до {payment_to:,} {currency}".replace(",", " ")
    return "не указана"


def _build_details(item: dict) -> str:
    parts = []
    if item.get("candidat"):
        parts.append(f"Требования:\n{_clean_text(item['candidat'])}")
    if item.get("work"):
        parts.append(f"Обязанности:\n{_clean_text(item['work'])}")
    if item.get("compensation"):
        parts.append(f"Условия:\n{_clean_text(item['compensation'])}")
    return "\n\n".join(part for part in parts if part).strip()


def _vacancy_identity(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').casefold()
        match = re.fullmatch(r'/vakansii/(?:[^/]*-)?([0-9]+)\.html/?', parsed.path)
        if (parsed.scheme != 'https' or not (host == 'superjob.ru' or host.endswith('.superjob.ru'))
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or not match):
            return ''
        return match[1]
    except (ValueError, TypeError, AttributeError):
        return ''


def _resume_title(item: dict) -> str:
    return (
        _clean_text(item.get("profession"))
        or _clean_text(item.get("last_profession"))
        or _clean_text(item.get("title"))
        or f"Resume #{item.get('id')}"
    )


def _ensure_dirs():
    os.makedirs(os.path.dirname(config.SUPERJOB_AUTH_FILE), exist_ok=True)
    os.makedirs(os.path.dirname(config.SUPERJOB_COOKIES_FILE), exist_ok=True)


def _load_cookies() -> list[dict] | None:
    return CookieRepository(config.SUPERJOB_COOKIES_FILE).snapshot()[0]


def _save_cookies(payload: list[dict]):
    _ensure_dirs()
    CookieRepository(config.SUPERJOB_COOKIES_FILE).save(payload)


def _load_auth_file() -> dict:
    return SuperJobAuthRepository(config.SUPERJOB_AUTH_FILE).snapshot()[0] or {}


def _save_auth_file(payload: dict):
    SuperJobAuthRepository(config.SUPERJOB_AUTH_FILE).save(payload)


class SuperJobClient:
    """Поиск вакансий и автоотклик через официальный API SuperJob."""

    def __init__(self):
        self._cookie_session = BrowserCookieSession(config.SUPERJOB_COOKIES_FILE, config.HH_STATE_DIR)
        self._auth_session = SuperJobAuthSession(config.SUPERJOB_AUTH_FILE)
        self._api_key = config.SUPERJOB_API_KEY
        self._client_id = config.SUPERJOB_CLIENT_ID
        self._api_base_url = config.SUPERJOB_API_BASE_URL
        self._configured_resume_id = config.SUPERJOB_RESUME_ID
        self._session: aiohttp.ClientSession | None = None
        self._session_uses_env_proxy = True
        self._auth: dict | None = None
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
                "X-Api-App-Id": self._api_key,
                "Accept": "application/json",
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
        proxy_url = os.environ.get("HH_PROXY") or config.BROWSER_PROXY
        if proxy_url:
            launch_opts["proxy"] = {"server": proxy_url}
            log.info("Using configured proxy for SuperJob browser")
        launch_opts["env"] = proxy_utils.browser_launch_env(proxy_url)

        await self._cookie_session.start(self, playwright_factory=async_playwright,
                                         launch_options=launch_opts, logger=log)

    async def stop_browser(self):
        await self._cookie_session.stop(self, logger=log)

    async def save_session(self):
        await self._cookie_session.save(self, logger=log)

    def _get_auth(self) -> dict:
        self._auth = self._auth_session.get()
        return self._auth

    def _save_auth(self):
        if self._auth is None:
            self._get_auth()
        self._auth_session.publish(self._auth)

    def _update_tokens(self, payload: dict, *, owner: str):
        self._auth_session.finish(owner, payload)
        self._auth = self._auth_session.get()

    def _mark_auth_uncertain(self, owner):
        try:
            self._auth_session.uncertain(owner)
        except Exception as exc:
            log.warning("SuperJob auth completion needs manual review: %s", type(exc).__name__)

    def _auth_header(self) -> str:
        token = self._get_auth().get("access_token", "")
        if self._auth.get("_token_attempt"):
            raise SuperJobAuthError("SuperJob token attempt needs manual review")
        token_type = self._get_auth().get("token_type", "bearer")
        if not token:
            return ""
        return f"{token_type.capitalize()} {token}"

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if path.startswith("/1.0/") or path.startswith("/2.0/"):
            return f"https://api.superjob.ru{path}"
        return f"{self._api_base_url}{path}"

    def _resume_id(self) -> int:
        if self._configured_resume_id > 0:
            return int(self._configured_resume_id)
        auth = self._get_auth()
        try:
            return int(auth.get("resume_id") or auth.get("user", {}).get("id_cv") or 0)
        except (TypeError, ValueError):
            return 0

    def _auth_is_fresh(self) -> bool:
        auth = self._get_auth()
        access_token = auth.get("access_token", "")
        expires_at = int(auth.get("expires_at") or 0)
        return bool(not auth.get("_token_attempt") and access_token and expires_at > int(time.time()) + 300)

    async def _decode_response(self, resp: aiohttp.ClientResponse) -> tuple[dict | list | None, str]:
        text = await resp.text()
        if not text.strip():
            return None, ""
        try:
            return json.loads(text), text
        except json.JSONDecodeError:
            return None, text

    def _extract_error_message(self, payload: dict | list | None, fallback: str) -> str:
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message") or error.get("error")
                if message:
                    return str(message)
            message = payload.get("message")
            if message:
                return str(message)
        return fallback

    async def _request_once(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        auth: bool = False,
        retry_on_auth_error: bool = True,
    ) -> dict | list | None:
        if not self._api_key:
            raise RuntimeError("SUPERJOB_API_KEY is not configured")

        headers = {}
        if auth:
            if not await self.ensure_auth():
                raise RuntimeError("Нет активной сессии SuperJob. Запусти ./run.sh superjob-login.")
            headers["Authorization"] = self._auth_header()

        url = self._build_url(path)
        async with self._session.request(
            method,
            url,
            params=params,
            data=data,
            headers=headers,
        ) as resp:
            payload, text = await self._decode_response(resp)

            if (
                auth
                and retry_on_auth_error
                and resp.status in {401, 404, 410}
                and self._get_auth().get("refresh_token")
            ):
                if await self.refresh_access_token():
                    return await self._request_once(
                        method,
                        path,
                        params=params,
                        data=data,
                        auth=auth,
                        retry_on_auth_error=False,
                    )

            if resp.status >= 400:
                # OAuth errors may echo tokens/credentials; never expose body/URL.
                raise RuntimeError(f"SuperJob API error {resp.status}")

            return payload

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        auth: bool = False,
        retry_on_auth_error: bool = True,
    ) -> dict | list | None:
        await self.start()
        try:
            return await self._request_once(
                method,
                path,
                params=params,
                data=data,
                auth=auth,
                retry_on_auth_error=retry_on_auth_error,
            )
        except Exception as exc:
            token_request = path in {"/oauth2/refresh_token/", "/oauth2/password/"}
            if not token_request and self._session_uses_env_proxy and proxy_utils.is_proxy_error(exc):
                log.warning("SuperJob proxy failed, retrying direct: %s", type(exc).__name__)
                await self.start(trust_env=False)
                return await self._request_once(
                    method,
                    path,
                    params=params,
                    data=data,
                    auth=auth,
                    retry_on_auth_error=retry_on_auth_error,
                )
            raise

    async def password_login(self, login: str, password: str):
        if not self._client_id:
            raise RuntimeError("SUPERJOB_CLIENT_ID is not configured")
        if not self._api_key:
            raise RuntimeError("SUPERJOB_API_KEY is not configured")
        owner = self._auth_session.begin("login")
        try:
            payload = await self._request(
                "POST",
                "/oauth2/password/",
                data={
                    "login": login,
                    "password": password,
                    "client_id": self._client_id,
                    "client_secret": self._api_key,
                    "hr": 0,
                },
                auth=False,
                retry_on_auth_error=False,
            )
            self._update_tokens(payload, owner=owner)
        except BaseException:
            self._mark_auth_uncertain(owner)
            raise

    async def refresh_access_token(self) -> bool:
        owner = None
        try:
            refresh_token = self._get_auth().get("refresh_token", "")
            if not refresh_token or not self._client_id or not self._api_key:
                return False
            owner = self._auth_session.begin("refresh")
            payload = await self._request(
                "GET",
                "/oauth2/refresh_token/",
                params={
                    "refresh_token": refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._api_key,
                },
                auth=False,
                retry_on_auth_error=False,
            )
            self._update_tokens(payload, owner=owner)
        except BaseException as exc:
            if owner is not None:
                self._mark_auth_uncertain(owner)
            if not isinstance(exc, Exception):
                raise
            log.warning("SuperJob token refresh failed: %s", type(exc).__name__)
            return False
        return True

    async def ensure_auth(self) -> bool:
        auth = self._get_auth()
        if self._auth_is_fresh():
            return True
        if auth.get("refresh_token"):
            return await self.refresh_access_token()
        return False

    async def get_current_user(self) -> dict:
        payload = await self._request("GET", "/user/current/", auth=True)
        return payload if isinstance(payload, dict) else {}

    async def get_user_resumes(self) -> list[dict]:
        payload = await self._request("GET", "/1.0/user_cvs/", auth=True)
        if isinstance(payload, dict):
            return payload.get("objects") or []
        return []

    async def _prompt_input(self, prompt: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: input(prompt).strip())

    async def _prompt_password(self, prompt: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: getpass.getpass(prompt).strip())

    async def _page_is_logged_in(self) -> bool:
        if self._page is None:
            return False
        if "/auth/login" in self._page.url or "/auth/password" in self._page.url:
            return False
        try:
            if await self._page.locator("a[href*='/user/responses/']").count() > 0:
                return True
            if await self._page.locator("a[href*='/user/resume/']").count() > 0:
                return True
        except Exception:
            pass
        try:
            body_text = (await self._page.locator("body").inner_text()).lower()
        except Exception:
            return False
        return "отклики и чаты" in body_text and "настройки" in body_text

    async def _choose_resume(self, current_user: dict, resumes: list[dict]) -> dict:
        if not resumes:
            resume_id = int(current_user.get("id_cv") or 0)
            if resume_id:
                return {"id": resume_id, "title": f"Resume #{resume_id}"}
            raise RuntimeError("У пользователя SuperJob не найдено резюме")

        primary_id = int(current_user.get("id_cv") or 0)
        default_idx = 0
        for idx, resume in enumerate(resumes):
            if int(resume.get("id") or 0) == primary_id:
                default_idx = idx
                break

        if len(resumes) == 1:
            return {
                "id": int(resumes[0].get("id") or 0),
                "title": _resume_title(resumes[0]),
            }

        print(f"\n📋 Найдено {len(resumes)} резюме в SuperJob:\n")
        for idx, resume in enumerate(resumes, 1):
            marker = " (основное)" if int(resume.get("id") or 0) == primary_id else ""
            print(f"  {idx}. {_resume_title(resume)}{marker}")
        print()

        answer = await self._prompt_input(
            f"Какое использовать для автоотклика? [1-{len(resumes)}] "
            f"(Enter = {default_idx + 1}): "
        )
        chosen_idx = default_idx
        if answer:
            try:
                parsed = int(answer) - 1
                if 0 <= parsed < len(resumes):
                    chosen_idx = parsed
            except ValueError:
                pass

        chosen = resumes[chosen_idx]
        return {
            "id": int(chosen.get("id") or 0),
            "title": _resume_title(chosen),
        }

    async def login_interactive(self):
        login = await self._prompt_input("Логин SuperJob (email или телефон): ")
        if not login:
            raise RuntimeError("Логин SuperJob не указан")

        password = await self._prompt_password("Пароль SuperJob: ")
        if not password:
            raise RuntimeError("Пароль SuperJob не указан")

        await self.start_browser(headless=False)
        try:
            await self._page.goto(
                config.SUPERJOB_LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self._page.wait_for_timeout(1200)
            await self._page.fill("input[name='login']", login)
            await self._page.locator("button[type='submit']").click()
            await self._page.wait_for_url("**/auth/password/**", timeout=30000)
            await self._page.wait_for_timeout(800)
            await self._page.fill("input[name='password']", password)
            await self._page.locator("button[type='submit']").click()
            await self._page.wait_for_timeout(5000)

            if not await self._page_is_logged_in():
                raise RuntimeError("Не удалось войти в SuperJob через браузерную сессию")

            await self.save_session()
            print("\n✅ SuperJob cookies сохранены!")
            print(f"   Пользователь: {login}")
            print(f"   Файл: {self._cookie_session.repository.path}")
        finally:
            await self.stop_browser()

    async def is_auto_apply_ready(self) -> bool:
        if not config.SUPERJOB_AUTO_APPLY:
            return False
        return await self.is_logged_in()

    async def is_logged_in(self) -> bool:
        if self._page is None:
            await self.start_browser()
        try:
            await self._page.goto(
                "https://www.superjob.ru/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self._page.wait_for_timeout(2000)
            return await self._page_is_logged_in()
        except Exception as exc:
            log.warning("SuperJob login check failed: %s", exc)
            return False

    async def search_vacancies(
        self,
        query: str,
        page: int = 0,
        profile: dict | None = None,
    ) -> tuple[list[dict], bool]:
        """Поиск вакансий. Возвращает нормализованный список и флаг more."""
        profile = profile or {}

        params: dict[str, str | int | list[int]] = {
            "keyword": query,
            "page": page,
            "count": config.SUPERJOB_COUNT_PER_PAGE,
        }

        if profile.get("town"):
            params["town"] = profile["town"]
        if profile.get("countries"):
            params["c"] = profile["countries"]
        if profile.get("place_of_work"):
            params["place_of_work"] = profile["place_of_work"]
        if config.SEARCH_SALARY:
            params["payment_from"] = config.SEARCH_SALARY
        if config.SEARCH_ONLY_WITH_SALARY:
            params["no_agreement"] = 1

        payload = await self._request("GET", "/vacancies/", params=params, auth=False)
        objects = payload.get("objects") if isinstance(payload, dict) else []
        vacancies = [self._normalize_vacancy(item) for item in (objects or [])]
        more = bool(payload.get("more")) if isinstance(payload, dict) else False
        return vacancies, more

    async def _arm_destination_boundary(self, control, expected):
        boundary_id = uuid.uuid4().hex
        await install_boundary(self._page, boundary_id)
        try:
            ok = await control.evaluate(r"""(control,expected) => {
            /* codex:native-destination-arm */
            __RUNTIME__
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
            const entries=submitter=>root.tagName==='FORM'?[...window.__jhActionBoundary.formData(root,submitter?.form===root&&submitter.type==='submit'?submitter:undefined).entries()]:[];
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
            const approval={id:expected.boundary_id,root,control,valid,blocked:false};
            const approvedPayload=payload(control);
            approval.eventMatches=(name,event,target)=>valid() &&
                (name==='click' ? target===control : event.target===root && event.submitter===control) &&
                contextMatches(name==='submit' ? event.submitter : control) &&
                payload(name==='submit' ? event.submitter : control)===approvedPayload;
            return window.__jhActionBoundary.register(approval);
        }""".replace("__RUNTIME__", RUNTIME + ";"), {'url':expected,'source':'superjob','boundary_id':boundary_id}) is True
        except BaseException:
            await release_boundary(self._page, boundary_id)
            raise
        self._destination_boundary_id = boundary_id
        if not ok:
            await release_boundary(self._page, boundary_id)
        return ok

    async def _destination_boundary_passed(self):
        boundary_id = self._destination_boundary_id
        try:
            return await self._page.evaluate('/* codex:native-destination-readback */ id => { const approval=window.__jhActionBoundary?.get(id);return !!approval && !approval.blocked; }', boundary_id) is True
        finally:
            await release_boundary(self._page, boundary_id)

    async def apply_to_vacancy(self, vacancy: dict, cover_letter: str = "") -> dict:
        vacancy_url = vacancy.get("url") or ""
        if not vacancy_url:
            return {"ok": False, "message": "Не найден URL вакансии SuperJob"}

        expected_id = _vacancy_identity(vacancy_url)
        supplied_id = str(vacancy.get('external_id') or vacancy.get('id') or '').removeprefix('superjob:')
        if not expected_id or (supplied_id and supplied_id != expected_id):
            return {'ok': False, 'reason': 'destination_unverified', 'message': 'ID/URL SuperJob не подтверждён'}
        if self._page is None:
            await self.start_browser()

        def destination_matches():
            return _vacancy_identity(self._page.url) == expected_id

        try:
            await self._page.goto(vacancy_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            log.warning("SuperJob vacancy navigation failed: %s", type(exc).__name__)
            return {'ok': False, 'reason': 'destination_unverified', 'message': 'Навигация SuperJob не подтверждена'}
        await self._page.wait_for_timeout(2500)
        if not destination_matches():
            return {'ok': False, 'reason': 'destination_unverified', 'message': 'Открыта другая вакансия SuperJob'}

        if not await self._page_is_logged_in():
            return {"ok": False, "message": "Не залогинен на SuperJob"}

        try:
            from private_artifacts import capture_artifacts
            await capture_artifacts(self._page, self._cookie_session.state_dir, "debug_superjob_apply_page", html=False)
        except Exception:
            pass

        body_text = (await self._page.locator("body").inner_text()).lower()
        if "перейти в чат" in body_text:
            return {"ok": True, "message": "Отклик уже существует"}
        if "заполнили анкету на ее сайте" in body_text:
            return {"ok": False, "message": "Работодатель требует анкету на своём сайте"}

        apply_btn = self._page.locator("button.f-test-vacancy-response-button").first
        if await apply_btn.count() == 0:
            return {"ok": False, "message": "Кнопка отклика не найдена"}

        if not destination_matches():
            return {'ok': False, 'reason': 'destination_unverified', 'message': 'Destination изменилась до apply'}
        if not await self._arm_destination_boundary(apply_btn, vacancy_url):
            return {'ok': False, 'reason': 'destination_unverified', 'message': 'Apply context не подтверждён'}
        if not await dispatch_approved(self._page,apply_btn,self._destination_boundary_id):
            return {'ok': False, 'reason': 'destination_unverified', 'message': 'Apply заблокирован browser boundary'}
        await self._page.wait_for_timeout(2500)

        if cover_letter:
            for selector in (
                "textarea[name*='cover']",
                "textarea[name*='letter']",
                "textarea",
            ):
                try:
                    field = self._page.locator(selector).first
                    if await field.count():
                        await field.fill(cover_letter[:1900])
                        break
                except Exception:
                    continue

        submit_btn = self._page.locator(
            "button[type='submit'].f-test-button-Otkliknutsya, button.f-test-button-Otkliknutsya[type='submit']"
        ).first
        try:
            if await submit_btn.count():
                if not destination_matches():
                    return {'ok': False, 'reason': 'destination_unverified', 'message': 'Destination изменилась до submit'}
                if not await self._arm_destination_boundary(submit_btn, vacancy_url):
                    return {'ok': False, 'reason': 'destination_unverified', 'message': 'Submit context не подтверждён'}
                if not await dispatch_approved(self._page,submit_btn,self._destination_boundary_id):
                    return {'ok': False, 'reason': 'destination_unverified', 'message': 'Submit заблокирован browser boundary'}
                await self._page.wait_for_timeout(3000)
        except Exception as exc:
            log.warning("SuperJob submit click failed: %s", exc)

        body_text = (await self._page.locator("body").inner_text()).lower()
        if "перейти в чат" in body_text:
            return {"ok": True, "message": "Отклик отправлен"}
        if "заполнили анкету на ее сайте" in body_text:
            return {"ok": False, "message": "Работодатель требует анкету на своём сайте"}
        if "отклик уже существует" in body_text:
            return {"ok": True, "message": "Отклик уже существует"}

        try:
            from private_artifacts import capture_artifacts
            await capture_artifacts(self._page, self._cookie_session.state_dir, "debug_superjob_apply_after_click")
        except Exception:
            pass

        return {"ok": False, "message": "Не удалось подтвердить отклик на SuperJob"}

    def _normalize_vacancy(self, item: dict) -> dict:
        town = item.get("town") or {}
        place = item.get("place_of_work") or {}
        published_at = item.get("date_published")

        location_parts = []
        if town.get("title"):
            location_parts.append(str(town["title"]))
        if place.get("title"):
            location_parts.append(str(place["title"]))

        title = _clean_text(item.get("profession")) or "Без названия"
        company = _clean_text(item.get("firm_name")) or "—"
        snippet_parts = [
            _clean_text(item.get("candidat")),
            _clean_text(item.get("work")),
        ]
        snippet = "\n".join(part for part in snippet_parts if part)[:1000]

        normalized = {
            "id": f"superjob:{item.get('id')}",
            "external_id": str(item.get("id") or ""),
            "source": "superjob",
            "source_label": "SuperJob",
            "title": title,
            "company": company,
            "salary": _format_salary(item),
            "url": item.get("link") or "",
            "snippet": snippet,
            "details": _build_details(item),
            "location": ", ".join(location_parts),
            "apply_mode": "auto" if config.SUPERJOB_AUTO_APPLY else "manual",
        }
        if published_at:
            normalized["published_at"] = datetime.fromtimestamp(
                int(published_at), tz=UTC
            ).isoformat()
        return normalized
