"""Клиент для поиска вакансий и автоотклика через SuperJob API."""
import asyncio
import getpass
import html
import json
import logging
import os
import re
import time
from datetime import UTC, datetime

import aiohttp
from playwright.async_api import async_playwright, BrowserContext, Page

import config
import proxy_utils

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
    if not os.path.exists(config.SUPERJOB_COOKIES_FILE):
        return None
    try:
        with open(config.SUPERJOB_COOKIES_FILE) as f:
            return json.load(f)
    except Exception as exc:
        log.warning("Failed to load SuperJob cookies: %s", exc)
        return None


def _save_cookies(payload: list[dict]):
    _ensure_dirs()
    with open(config.SUPERJOB_COOKIES_FILE, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _load_auth_file() -> dict:
    if not os.path.exists(config.SUPERJOB_AUTH_FILE):
        return {}
    try:
        with open(config.SUPERJOB_AUTH_FILE) as f:
            return json.load(f)
    except Exception as exc:
        log.warning("Failed to load SuperJob auth file: %s", exc)
        return {}


def _save_auth_file(payload: dict):
    _ensure_dirs()
    with open(config.SUPERJOB_AUTH_FILE, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


class SuperJobClient:
    """Поиск вакансий и автоотклик через официальный API SuperJob."""

    def __init__(self):
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
                "X-Api-App-Id": config.SUPERJOB_API_KEY,
                "Accept": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=20),
            trust_env=trust_env,
        )
        self._session_uses_env_proxy = trust_env

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
        proxy_url = os.environ.get("HH_PROXY") or config.BROWSER_PROXY
        if proxy_url:
            launch_opts["proxy"] = {"server": proxy_url}
            log.info("Using proxy for SuperJob browser: %s", proxy_url)
        launch_opts["env"] = proxy_utils.browser_launch_env(proxy_url)

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
            log.info("Loaded %d SuperJob cookies", len(cookies))

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
            log.info("SuperJob session saved (%d cookies)", len(cookies))

    def _get_auth(self) -> dict:
        if self._auth is None:
            self._auth = _load_auth_file()
        return self._auth

    def _save_auth(self):
        _save_auth_file(self._get_auth())

    def _update_tokens(self, payload: dict):
        auth = self._get_auth()
        expires_at = int(payload.get("ttl") or 0)
        if not expires_at:
            expires_at = int(time.time()) + int(payload.get("expires_in") or 0)
        auth.update(
            {
                "access_token": payload.get("access_token", ""),
                "refresh_token": payload.get("refresh_token", auth.get("refresh_token", "")),
                "token_type": payload.get("token_type", "bearer"),
                "expires_at": expires_at,
            }
        )
        self._save_auth()

    def _auth_header(self) -> str:
        token = self._get_auth().get("access_token", "")
        token_type = self._get_auth().get("token_type", "bearer")
        if not token:
            return ""
        return f"{token_type.capitalize()} {token}"

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if path.startswith("/1.0/") or path.startswith("/2.0/"):
            return f"https://api.superjob.ru{path}"
        return f"{config.SUPERJOB_API_BASE_URL}{path}"

    def _resume_id(self) -> int:
        if config.SUPERJOB_RESUME_ID > 0:
            return int(config.SUPERJOB_RESUME_ID)
        auth = self._get_auth()
        try:
            return int(auth.get("resume_id") or auth.get("user", {}).get("id_cv") or 0)
        except (TypeError, ValueError):
            return 0

    def _auth_is_fresh(self) -> bool:
        auth = self._get_auth()
        access_token = auth.get("access_token", "")
        expires_at = int(auth.get("expires_at") or 0)
        return bool(access_token and expires_at > int(time.time()) + 300)

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
        if not config.SUPERJOB_API_KEY:
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
                fallback = f"SuperJob API error {resp.status}"
                if text:
                    fallback = f"{fallback}: {text[:300]}"
                raise RuntimeError(self._extract_error_message(payload, fallback))

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
            if self._session_uses_env_proxy and proxy_utils.is_proxy_error(exc):
                log.warning("SuperJob proxy failed, retrying direct: %s", exc)
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
        if not config.SUPERJOB_CLIENT_ID:
            raise RuntimeError("SUPERJOB_CLIENT_ID is not configured")

        payload = await self._request(
            "POST",
            "/oauth2/password/",
            data={
                "login": login,
                "password": password,
                "client_id": config.SUPERJOB_CLIENT_ID,
                "client_secret": config.SUPERJOB_API_KEY,
                "hr": 0,
            },
            auth=False,
            retry_on_auth_error=False,
        )
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise RuntimeError("SuperJob did not return access_token")
        self._update_tokens(payload)

    async def refresh_access_token(self) -> bool:
        refresh_token = self._get_auth().get("refresh_token", "")
        if not refresh_token or not config.SUPERJOB_CLIENT_ID:
            return False
        try:
            payload = await self._request(
                "GET",
                "/oauth2/refresh_token/",
                params={
                    "refresh_token": refresh_token,
                    "client_id": config.SUPERJOB_CLIENT_ID,
                    "client_secret": config.SUPERJOB_API_KEY,
                },
                auth=False,
                retry_on_auth_error=False,
            )
        except Exception as exc:
            log.warning("SuperJob token refresh failed: %s", exc)
            return False

        if not isinstance(payload, dict) or not payload.get("access_token"):
            return False
        self._update_tokens(payload)
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
            print(f"   Файл: {config.SUPERJOB_COOKIES_FILE}")
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

    async def apply_to_vacancy(self, vacancy: dict, cover_letter: str = "") -> dict:
        vacancy_url = vacancy.get("url") or ""
        if not vacancy_url:
            return {"ok": False, "message": "Не найден URL вакансии SuperJob"}

        if self._page is None:
            await self.start_browser()

        try:
            await self._page.goto(vacancy_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            log.warning("SuperJob vacancy nav issue: %s", exc)
        await self._page.wait_for_timeout(2500)

        if not await self._page_is_logged_in():
            return {"ok": False, "message": "Не залогинен на SuperJob"}

        try:
            await self._page.screenshot(
                path=os.path.join(config.HH_STATE_DIR, "debug_superjob_apply_page.png")
            )
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

        await apply_btn.click()
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
                await submit_btn.click()
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
            await self._page.screenshot(
                path=os.path.join(config.HH_STATE_DIR, "debug_superjob_apply_after_click.png")
            )
            with open(
                os.path.join(config.HH_STATE_DIR, "debug_superjob_apply_after_click.html"),
                "w",
                encoding="utf-8",
            ) as f:
                f.write(await self._page.content())
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
