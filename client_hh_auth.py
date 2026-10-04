"""HH auth and resume import flow for client profiles."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import html
import json
import logging
import os
import re
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit

import config
import profile as profile_mod
from hh_client import HHClient
from state_store.json_store import atomic_write_json, atomic_write_text, file_lock
from state_store.hh_resume_import import HHResumeImport, ResumeCatalogRepository

log = logging.getLogger(__name__)

_TRANSIENT_HH_NAVIGATION_ERRORS = (
    "net::ERR_CONNECTION_CLOSED",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_CONNECTION_REFUSED",
    "net::ERR_TIMED_OUT",
    "net::ERR_NAME_NOT_RESOLVED",
)


def _is_transient_hh_navigation_error(exc: Exception) -> bool:
    return any(marker in str(exc) for marker in _TRANSIENT_HH_NAVIGATION_ERRORS)


async def _open_hh_login(page, url: str, *, attempts: int = 3) -> None:
    """Open HH login, tolerating short-lived local network disconnects."""
    for attempt in range(max(1, attempts)):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return
        except Exception as exc:
            if not _is_transient_hh_navigation_error(exc) or attempt + 1 >= attempts:
                raise
            await asyncio.sleep(2 * (attempt + 1))


def _resolve_profile(profile_name: str):
    try:
        return profile_mod.load_profile(profile_name)
    except FileNotFoundError:
        active = profile_mod.load_profile()
        active_home = os.path.normpath(active.home_dir)
        if active_home.endswith(os.path.join("profiles", profile_name)) or os.path.basename(active_home) == profile_name:
            active.name = profile_name
            return active
        raise


def hh_resume_catalog_path(profile_name: str) -> str:
    profile = _resolve_profile(profile_name)
    return os.path.join(profile.home_dir, "hh_resumes.json")


def hh_resume_exports_dir(profile_name: str) -> str:
    profile = _resolve_profile(profile_name)
    return os.path.join(profile.home_dir, "hh_resumes")


def load_hh_resume_catalog(profile_name: str) -> list[dict]:
    path = hh_resume_catalog_path(profile_name)
    return ResumeCatalogRepository(path).snapshot()[0] or []


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", (value or "").strip()).strip("_")
    return slug or "resume"


def _write_text(path: str, text: str) -> str:
    atomic_write_text(path, text.rstrip() + "\n")
    return path


def _read_env_values(path: str, keys: tuple[str, ...]) -> dict[str, str]:
    if not path or not os.path.isfile(path):
        return {}
    wanted = set(keys)
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            if key not in wanted:
                continue
            values[key] = value.strip().strip("'\"")
    return values


def _load_hh_auth_env(profile_name: str) -> dict[str, str]:
    """Named profiles use only their own login hints, never shared credentials."""
    profile_mod.validate_profile_name(profile_name)
    if profile_name != "default":
        profile = _resolve_profile(profile_name)
        return _read_env_values(os.path.join(profile.home_dir, "profile.env"), HH_AUTH_LOGIN_ENV_KEYS)
    env_file = os.getenv("JOB_HUNTER_ENV_FILE", "").strip() or os.path.expanduser("~/.job-hunter/job-hunter.env")
    values = _read_env_values(env_file, HH_AUTH_LOGIN_ENV_KEYS)
    for key in HH_AUTH_LOGIN_ENV_KEYS:
        process_value = os.getenv(key, "").strip()
        if process_value and not values.get(key):
            values[key] = process_value
    return values


def _normalize_env_value(value: str | int | None) -> str:
    raw = "" if value is None else str(value)
    return re.sub(r"\s+", " ", raw).strip()


def _save_resume_catalog(profile_name: str, items: list[dict]) -> str:
    path = hh_resume_catalog_path(profile_name)
    ResumeCatalogRepository(path).save(items)
    return path


_QA_RESUME_TITLE_RE = re.compile(r"\bqa\b|quality assurance|manual qa|test engineer|тестиров", re.I)
_NON_QA_RESUME_TITLE_RE = re.compile(r"электрик|электромонтаж|электро|сервисн", re.I)


def _clean_resume_title(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _is_valid_resume_item(item: dict) -> bool:
    return bool(str(item.get("id") or "").strip())


def _is_relevant_resume_for_profile(profile_name: str, item: dict) -> bool:
    title = _clean_resume_title(str(item.get("title") or ""))
    profile_key = (profile_name or "").strip().casefold()
    if profile_key in {"qa", "test", "tester", "manual_qa"}:
        return bool(_QA_RESUME_TITLE_RE.search(title)) and not bool(_NON_QA_RESUME_TITLE_RE.search(title))
    return True


def _select_profile_resumes(profile_name: str, resumes: list[dict]) -> list[dict]:
    selected = []
    seen_ids = set()
    for item in resumes:
        resume_id = str(item.get("id") or "").strip()
        if not resume_id or resume_id in seen_ids:
            continue
        seen_ids.add(resume_id)
        if not _is_relevant_resume_for_profile(profile_name, item):
            continue
        clean = dict(item)
        clean["id"] = resume_id
        clean["title"] = _clean_resume_title(str(clean.get("title") or resume_id))
        selected.append(clean)
    return selected


def _update_profile_resume_ids(profile_name: str, resumes: list[dict], *, env_file=None, expected_content=...) -> str:
    if env_file is None:
        profile = _resolve_profile(profile_name)
        env_file = os.path.join(profile.home_dir, "profile.env")
    if not os.path.isfile(env_file):
        raise FileNotFoundError(f"Профиль '{profile_name}' не найден: {env_file}")

    updates: dict[str, str] = {}
    slots = (
        ("HH_PRIMARY_RESUME_ID", "HH_PRIMARY_RESUME_TITLE"),
        ("HH_SECONDARY_RESUME_ID", "HH_SECONDARY_RESUME_TITLE"),
        ("HH_TERTIARY_RESUME_ID", "HH_TERTIARY_RESUME_TITLE"),
    )
    for idx, (id_key, title_key) in enumerate(slots):
        item = resumes[idx] if idx < len(resumes) else {"id": "", "title": ""}
        updates[id_key] = _normalize_env_value(item.get("id") or "")
        updates[title_key] = _normalize_env_value(item.get("title") or "")

    return profile_mod.update_env_file(env_file, updates, expected_content=expected_content)



HH_AUTH_LOGIN_ENV_KEYS = (
    "HH_AUTH_LOGIN",
    "HH_LOGIN",
    "HH_AUTH_PHONE",
    "HH_PHONE",
    "HH_AUTH_EMAIL",
    "HH_EMAIL",
)

HH_AUTH_LOGIN_INPUT_SELECTORS = (
    "input[name='login']",
    "input[name='username']",
    "input[type='email']",
    "input[type='tel']",
    "input[inputmode='tel'][data-qa='magritte-phone-input-national-number-input']",
    "input[data-qa='magritte-phone-input-national-number-input']",
    "input[autocomplete='username']",
    "input[data-qa*='login' i]:not([type='password'])",
    "input[placeholder*='телефон' i]",
    "input[placeholder*='почт' i]",
    "input[placeholder*='email' i]",
)

HH_AUTH_CODE_INPUT_SELECTORS = (
    "input[autocomplete='one-time-code']",
    "input[name*='code' i]",
    "input[id*='code' i]",
    "input[data-qa*='code' i]",
    "input[placeholder*='код' i]",
    "input[inputmode='numeric']",
    "input[type='tel']",
    "input[type='text']",
)

HH_AUTH_CONTINUE_SELECTORS = (
    "button:has-text('Далее')",
    "button:has-text('Продолжить')",
    "button:has-text('Получить код')",
    "button:has-text('Выслать код')",
    "button:has-text('Отправить код')",
    "button:has-text('Войти')",
    "button:has-text('Подтвердить')",
    "button[type='submit']",
    "input[type='submit']",
)

HH_AUTH_ROLE_INPUT_SELECTORS = (
    "input[name=\"account-type\"][data-qa*=\"APPLICANT\"]",
    "input[data-qa*=\"account-type-card-APPLICANT\"]",
)

HH_AUTH_ROLE_SUBMIT_SELECTORS = (
    "form[data-qa=\"account-login-form\"] button[data-qa=\"submit-button\"]",
)

HH_AUTH_PASSWORD_INPUT_SELECTORS = (
    "input[type=\"password\"]",
    "input[name=\"password\"]",
    "input[data-qa=\"applicant-login-input-password\"]",
)

HH_AUTH_CODE_MODE_SELECTORS = (
    "button[data-qa=\"expand-login-by-code-text\"]",
    "button:has-text(\"Войти по коду\")",
)

HH_AUTH_PHONE_MODE_SELECTORS = (
    "button:has-text('Телефон')",
    "a:has-text('Телефон')",
    "button:has-text('по телефону')",
    "a:has-text('по телефону')",
)

HH_AUTH_CAPTCHA_SELECTORS = (
    "iframe[src*='captcha']",
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='smartcaptcha']",
    "input[placeholder*='Текст с картинки' i]",
    "input[name='captcha' i]:not([type='hidden'])",
    "input[name*='captcha' i]:not([type='hidden'])",
    "input[id*='captcha' i]:not([type='hidden'])",
    "img[alt='captcha' i]",
    "img[data-qa*='captcha' i]",
    "[data-qa='captcha']",
    "[data-qa*='captcha-picture' i]",
)

HH_AUTH_BLOCKED_TEXT_TOKENS = (
    "обязательное поле",
    "заполните поле",
    "укажите телефон",
    "укажите email",
    "укажите почту",
    "некорректный телефон",
    "некорректный email",
    "неверный телефон",
    "неверный email",
    "проверьте телефон",
    "проверьте email",
    "слишком много попыток",
    "попробуйте позже",
)

HH_AUTH_STEP_IDLE = "idle"
HH_AUTH_STEP_SUBMITTED = "submitted"
HH_AUTH_STEP_PROGRESS = "progress"
HH_AUTH_STEP_STALLED = "stalled"
HH_AUTH_STEP_BLOCKED = "blocked"
HH_AUTH_STEP_CAPTCHA = "captcha"


def _resolve_hh_auth_login(auth_env: dict[str, str] | None = None) -> str:
    values = auth_env if auth_env is not None else os.environ
    for key in HH_AUTH_LOGIN_ENV_KEYS:
        value = str(values.get(key) or "").strip()
        if value:
            return value
    return ""


def _normalize_hh_auth_code(value: str) -> str:
    return "".join(re.findall(r"\d", value or ""))


def _looks_like_hh_auth_code_prompt(text: str, url: str = "") -> bool:
    haystack = f"{text or ''} {url or ''}".casefold()
    if not any(token in haystack for token in ("код", "sms", "смс", "однораз")):
        return False
    return any(
        token in haystack
        for token in (
            "код из смс",
            "код из sms",
            "код подтверждения",
            "одноразовый код",
            "введите код",
            "введи код",
            "код отправлен",
            "отправили код",
            "пришел код",
            "пришёл код",
            "sms-код",
            "смс-код",
        )
    )


def _looks_like_hh_auth_login_prompt(text: str, url: str = "") -> bool:
    haystack = f"{text or ''} {url or ''}".casefold()
    if _looks_like_hh_auth_code_prompt(haystack, url):
        return False
    if "/account/login" in haystack or "/auth/" in haystack:
        return True
    if any(token in haystack for token in ("номер телефона", "телефон или email", "телефон или почт", "введите телефон", "введите номер")):
        return True
    return "войти" in haystack and any(token in haystack for token in ("телефон", "почт", "email", "логин"))


def _looks_like_hh_auth_captcha(text: str, url: str = "") -> bool:
    haystack = f"{text or ''} {url or ''}".casefold()
    if "/account/captcha" in haystack:
        return True
    return any(
        token in haystack
        for token in (
            "подтвердите, что вы не робот",
            "текст с картинки",
            "введите текст с картинки",
            "i'm not a robot",
            "verify you are human",
            "не удалось проверить ваш браузер автоматически",
        )
    )


def _looks_like_hh_auth_blocked(text: str) -> bool:
    haystack = (text or "").casefold()
    return any(token in haystack for token in HH_AUTH_BLOCKED_TEXT_TOKENS)


def _hh_auth_page_signature(url: str, text: str) -> str:
    compact_text = " ".join((text or "").split())
    return f"{url or ''}\n{compact_text[:3000]}"


async def _hh_auth_page_text(page) -> str:
    try:
        return " ".join((await page.locator("body").inner_text(timeout=1500)).split()).casefold()
    except Exception:
        try:
            raw = await page.content()
        except Exception:
            return ""
        raw = re.sub(r"<[^>]+>", " ", raw)
        return " ".join(raw.split()).casefold()


async def _first_visible_locator(page, selectors: tuple[str, ...]):
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
        except Exception:
            continue
        for idx in range(min(int(count or 0), 12)):
            item = locator.nth(idx)
            try:
                if await item.is_visible(timeout=500):
                    return item
            except TypeError:
                try:
                    if await item.is_visible():
                        return item
                except Exception:
                    continue
            except Exception:
                continue
    return None


async def _mutation_allowed(guard=None, owner_guard=None):
    if guard is not None and not await guard():
        return False
    return owner_guard is None or owner_guard()


async def _fill_first_visible(page, selectors: tuple[str, ...], value: str, *, guard=None, owner_guard=None, receipts=None) -> bool:
    item = await _first_visible_locator(page, selectors)
    if not item:
        return False
    if not await _mutation_allowed(guard, owner_guard):
        return False
    try:
        await item.fill(value, timeout=5000)
    except TypeError:
        if not await _mutation_allowed(guard, owner_guard):
            return False
        await item.fill(value)
    if receipts is not None:
        receipts.append((item, value))
    return True


async def _fill_hh_auth_login(page, value: str, *, guard=None, owner_guard=None, receipts=None) -> bool:
    phone_input = await _first_visible_locator(
        page,
        (
            "input[data-qa=\"magritte-phone-input-national-number-input\"]",
            "input[inputmode=\"tel\"]",
        ),
    )
    digits = _normalize_hh_auth_code(value)
    if phone_input and digits:
        # HH renders +7 in a separate calling-code input.
        national = digits[1:] if len(digits) == 11 and digits[0] in "78" else digits
        if not await _mutation_allowed(guard, owner_guard):
            return False
        try:
            await phone_input.fill(national, timeout=5000)
        except TypeError:
            if not await _mutation_allowed(guard, owner_guard):
                return False
            await phone_input.fill(national)
        if receipts is not None:
            receipts.append((phone_input, national))
        return True
    return await _fill_first_visible(page, HH_AUTH_LOGIN_INPUT_SELECTORS, value,
                                    guard=guard, owner_guard=owner_guard, receipts=receipts)


async def _click_first_visible(page, selectors: tuple[str, ...], *, guard=None, owner_guard=None) -> bool:
    item = await _first_visible_locator(page, selectors)
    if not item:
        return False
    if not await _mutation_allowed(guard, owner_guard):
        return False
    try:
        await item.click(timeout=5000)
    except TypeError:
        if not await _mutation_allowed(guard, owner_guard):
            return False
        await item.click()
    except Exception as exc:
        if "captcha" in str(exc).casefold():
            raise RuntimeError("captcha_intercepted_click") from exc
        raise
    return True


async def _has_hh_auth_captcha_marker(page, text: str, url: str) -> bool:
    if _looks_like_hh_auth_captcha(text, url):
        return True
    return await _first_visible_locator(page, HH_AUTH_CAPTCHA_SELECTORS) is not None


async def _page_has_hh_auth_prompt(page) -> bool:
    if page is None or page.is_closed():
        return False
    url = str(getattr(page, "url", "") or "")
    text = await _hh_auth_page_text(page)

    return (
        _looks_like_hh_auth_login_prompt(text, url)
        or _looks_like_hh_auth_code_prompt(text, url)
        or await _has_hh_auth_captcha_marker(page, text, url)
        or await _first_visible_locator(page, HH_AUTH_LOGIN_INPUT_SELECTORS) is not None
    )


async def _solve_hh_auth_captcha(client: HHClient, profile_name: str, *, stage: str) -> dict:
    page = client._page  # noqa: SLF001 - auth flow owns the page lifecycle
    page_url = str(getattr(page, "url", "") or "")
    try:
        import captcha_solver
        from hh_client import _get_question_answer_client

        kind = await captcha_solver.handle_anti_bot_with_solver(
            client,
            _get_question_answer_client,
            "captcha",
            stage=stage,
        )
    except Exception as exc:
        return {
            "status": HH_AUTH_STEP_CAPTCHA,
            "detail": f"Captcha solver недоступен: {exc}",
            "url": page_url,
            "solver_attempted": False,
        }
    if kind != "captcha":
        return {
            "status": HH_AUTH_STEP_PROGRESS,
            "detail": "HH captcha снята через solver/Telegram bridge.",
            "url": str(getattr(page, "url", "") or page_url),
            "solver_attempted": True,
        }
    return {
        "status": HH_AUTH_STEP_CAPTCHA,
        "detail": "HH captcha не снята после vision/Telegram bridge.",
        "url": str(getattr(page, "url", "") or page_url),
        "solver_attempted": True,
    }


async def _wait_for_hh_auth_progress(page, before_url: str, before_text: str, *, timeout_ms: int = 7000) -> dict:
    before_url_base = (before_url or "").split("#", 1)[0]
    before_was_login = _looks_like_hh_auth_login_prompt(before_text, before_url)
    deadline = time.monotonic() + max(0.5, timeout_ms / 1000)
    last_url = before_url
    last_text = before_text

    while time.monotonic() <= deadline:
        try:
            await page.wait_for_timeout(500)
        except Exception:
            await asyncio.sleep(0.5)
        last_url = str(getattr(page, "url", "") or "")
        last_url_base = last_url.split("#", 1)[0]
        last_text = await _hh_auth_page_text(page)
        if await _has_hh_auth_captcha_marker(page, last_text, last_url):
            return {"status": HH_AUTH_STEP_CAPTCHA, "detail": "HH показал captcha после отправки формы."}
        if _looks_like_hh_auth_code_prompt(last_text, last_url):
            return {"status": HH_AUTH_STEP_PROGRESS, "detail": "HH перешёл к вводу SMS-кода."}
        if _looks_like_hh_auth_blocked(last_text):
            return {"status": HH_AUTH_STEP_BLOCKED, "detail": "HH показал ошибку в форме логина."}
        if last_url_base and before_url_base and last_url_base != before_url_base:
            return {"status": HH_AUTH_STEP_PROGRESS, "detail": "HH сменил URL после отправки формы."}
        if before_was_login and not _looks_like_hh_auth_login_prompt(last_text, last_url):
            return {"status": HH_AUTH_STEP_PROGRESS, "detail": "HH убрал форму логина после отправки."}

    return {
        "status": HH_AUTH_STEP_STALLED,
        "detail": "После отправки формы HH не перешёл к SMS-коду, captcha или новой странице.",
        "url": last_url,
    }


async def _submit_hh_auth_form(page, *, guard=None, owner_guard=None) -> dict:
    try:
        clicked = await _click_first_visible(page, HH_AUTH_CONTINUE_SELECTORS, guard=guard, owner_guard=owner_guard)
    except RuntimeError as exc:
        if str(exc) == "captcha_intercepted_click":
            return {"status": HH_AUTH_STEP_CAPTCHA, "detail": "Captcha modal перекрыла кнопку отправки."}
        raise
    if clicked:
        return {"status": HH_AUTH_STEP_SUBMITTED}
    try:
        if not await _mutation_allowed(guard, owner_guard):
            return {"status": HH_AUTH_STEP_BLOCKED, "detail": "Попытка входа HH изменилась; форма не отправлена."}
        await page.keyboard.press("Enter")
        return {"status": HH_AUTH_STEP_SUBMITTED}
    except Exception:
        return {"status": HH_AUTH_STEP_IDLE, "detail": "Не удалось отправить форму HH."}


async def _fill_hh_auth_code(page, code: str, *, guard=None, owner_guard=None, receipts=None) -> bool:
    digits = _normalize_hh_auth_code(code)
    if not digits:
        return False
    if await _fill_first_visible(page, HH_AUTH_CODE_INPUT_SELECTORS[:6], digits,
                                 guard=guard, owner_guard=owner_guard, receipts=receipts):
        return True
    try:
        locator = page.locator("input[type='text'], input[type='tel'], input[inputmode='numeric']")
        visible = []
        for idx in range(min(await locator.count(), 12)):
            item = locator.nth(idx)
            if await item.is_visible():
                visible.append(item)
        if len(visible) >= len(digits) >= 4:
            for item, digit in zip(visible, digits):
                if not await _mutation_allowed(guard, owner_guard):
                    return False
                await item.fill(digit)
                if receipts is not None:
                    receipts.append((item, digit))
            return True
    except Exception:
        pass
    return await _fill_first_visible(page, HH_AUTH_CODE_INPUT_SELECTORS, digits,
                                    guard=guard, owner_guard=owner_guard, receipts=receipts)


async def _notify_hh_auth_warning(profile_name: str, title: str, detail: str, *, page_url: str = "") -> bool:
    try:
        import notifier
        safe_profile = html.escape(profile_name or "default")
        safe_title = html.escape(title)
        safe_detail = html.escape(detail)
        text = (
            f"⚠️ <b>{safe_title}</b>\n\n"
            f"Профиль: <code>{safe_profile}</code>\n"
            f"{safe_detail}"
        )
        if page_url:
            text += f"\nURL: <code>{html.escape(page_url[:220])}</code>"
        return await notifier.send_message(text)
    except Exception:
        return False


async def _notify_hh_auth_request(kind: str, profile_name: str, prompt: str, timeout_s: int) -> bool:
    try:
        import notifier
        safe_profile = html.escape(profile_name or "default")
        safe_prompt = html.escape(prompt)
        if kind == "code":
            text = (
                "🔐 <b>HH просит SMS-код</b>\n\n"
                f"Профиль: <code>{safe_profile}</code>\n"
                f"{safe_prompt}\n\n"
                "Пришли код ответным сообщением в этот чат. Только цифры."
            )
        else:
            text = (
                "🔐 <b>HH просит логин</b>\n\n"
                f"Профиль: <code>{safe_profile}</code>\n"
                f"{safe_prompt}\n\n"
                "Пришли телефон или email ответным сообщением."
            )
        if timeout_s:
            text += f"\nОкно ожидания: {max(1, int(timeout_s // 60))} мин."
        return await notifier.send_message(text)
    except Exception:
        return False


async def _request_hh_auth_value(kind: str, profile_name: str, prompt: str, *, page_url: str, timeout_s: int, poll_sec: float) -> str:
    import hh_auth_bridge

    request_id = hh_auth_bridge.create_request(
        kind=kind,
        profile_name=profile_name,
        prompt=prompt,
        page_url=page_url,
        timeout_s=timeout_s,
    )
    try:
        await _notify_hh_auth_request(kind, profile_name, prompt, timeout_s)
        answer = await hh_auth_bridge.wait_for_response(
            request_id,
            timeout_s=timeout_s,
            poll_interval_s=poll_sec,
            profile_name=profile_name,
        )
        return (answer or "").strip()
    finally:
        hh_auth_bridge.complete_request(request_id, profile_name=profile_name)


async def _drive_hh_auth_step(
    client: HHClient,
    profile_name: str,
    *,
    timeout_s: int,
    poll_sec: float,
    auth_login: str = "",
) -> dict:
    page = client._page  # noqa: SLF001 - auth flow owns the page lifecycle
    if page is None or page.is_closed():
        return {"status": HH_AUTH_STEP_IDLE, "detail": "Окно HH закрыто."}

    context = getattr(client, "_context", None)
    binding = getattr(client, "_cookie_binding", None)
    revision = getattr(binding, "revision", None)
    write_nonce = getattr(client, "_cookie_write_nonce", None)
    step_nonce = object()
    client._auth_step_nonce = step_nonce
    prompt_identity = None
    receipts = []

    def same_owner():
        if (client._page is not page or getattr(client, "_context", None) is not context
                or page.is_closed() or getattr(client, "_auth_step_nonce", None) is not step_nonce
                or getattr(client, "_cookie_write_nonce", None) is not write_nonce
                or getattr(client, "_cookie_binding", None) is not binding):
            return False
        if binding is not None:
            if (binding.context is not context or binding.revoked or binding.closing
                    or binding.revision != revision):
                return False
            try:
                return binding.repository.snapshot()[1] == revision
            except Exception:
                return False
        return True

    async def current_prompt(kind):
        if not same_owner():
            return False
        current_url = str(getattr(page, "url", "") or "")
        parsed = urlsplit(current_url)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or not (parsed.hostname == "hh.ru" or (parsed.hostname or "").endswith(".hh.ru"))
                or not (parsed.path.startswith("/account/login") or parsed.path.startswith("/auth/"))):
            return False
        if await _first_visible_locator(page, HH_AUTH_PASSWORD_INPUT_SELECTORS):
            return False
        visible_login = (await _first_visible_locator(page, HH_AUTH_LOGIN_INPUT_SELECTORS)
                         if kind == "login" else None)
        # No locator await may follow this final challenge/account observation.
        current_text = await _hh_auth_page_text(page)
        matches = (_looks_like_hh_auth_code_prompt(current_text, current_url) if kind == "code"
                   else (_looks_like_hh_auth_login_prompt(current_text, current_url)
                         or bool(visible_login)))
        return (same_owner() and current_url == str(getattr(page, "url", "") or "") and matches
                and (prompt_identity is None or prompt_identity == (current_url, current_text)))

    async def capture_prompt(kind):
        nonlocal prompt_identity
        if not await current_prompt(kind):
            return False
        captured_url = str(getattr(page, "url", "") or "")
        captured_text = await _hh_auth_page_text(page)
        prompt_identity = (captured_url, captured_text)
        return await current_prompt(kind)

    def mutation_owner():
        return (same_owner() and prompt_identity is not None
                and str(getattr(page, "url", "") or "") == prompt_identity[0])

    async def credential_guard(kind):
        if not await current_prompt(kind):
            return False
        try:
            for item, expected in receipts:
                if await item.input_value() != expected:
                    return False
        except Exception:
            return False
        return await current_prompt(kind) and mutation_owner()

    stale = {"status": HH_AUTH_STEP_BLOCKED, "detail": "Окно, аккаунт или попытка входа HH изменились; ответ не отправлен."}

    url = str(getattr(page, "url", "") or "")
    text = await _hh_auth_page_text(page)

    async def control_prompt_unchanged():
        if not same_owner() or str(getattr(page, "url", "") or "") != url:
            return False
        fresh_text = await _hh_auth_page_text(page)
        return same_owner() and str(getattr(page, "url", "") or "") == url and fresh_text == text

    # HH can remember the account and open its password form. Switch by
    # the exact secondary button and leave this iteration immediately: no
    # generic submit is allowed until a phone or one-time code was filled.
    code_mode_button = await _first_visible_locator(page, HH_AUTH_CODE_MODE_SELECTORS)
    if code_mode_button:
        if not await control_prompt_unchanged():
            return stale
        try:
            await code_mode_button.click(timeout=5000)
        except TypeError:
            if not await control_prompt_unchanged():
                return stale
            await code_mode_button.click()
        await page.wait_for_timeout(1000)
        return {
            "status": HH_AUTH_STEP_PROGRESS,
            "detail": "HH переключён с пароля на вход по одноразовому коду.",
            "url": str(getattr(page, "url", "") or url),
        }
    if await _first_visible_locator(page, HH_AUTH_PASSWORD_INPUT_SELECTORS):
        return {
            "status": HH_AUTH_STEP_BLOCKED,
            "detail": "HH открыл вход по паролю, но кнопка входа по коду не найдена.",
            "url": url,
        }

    # The first HH screen only selects applicant/employer. Its submit button
    # is safe without a text value, but only while the applicant radio exists.
    role = await _first_visible_locator(page, HH_AUTH_ROLE_INPUT_SELECTORS)
    if role:
        async def selected_role_guard():
            if not same_owner():
                return False
            try:
                current_role = await _first_visible_locator(page, HH_AUTH_ROLE_INPUT_SELECTORS)
                if current_role is None or not await current_role.is_checked():
                    return False
                if await _first_visible_locator(page, HH_AUTH_PASSWORD_INPUT_SELECTORS):
                    return False
            except Exception:
                return False
            return await control_prompt_unchanged()
        before_url = str(getattr(page, "url", "") or "")
        before_text = await _hh_auth_page_text(page)
        if not await _click_first_visible(page, HH_AUTH_ROLE_SUBMIT_SELECTORS,
                                          guard=selected_role_guard, owner_guard=same_owner):
            return {
                "status": HH_AUTH_STEP_BLOCKED,
                "detail": "На экране выбора роли HH не найдена кнопка «Войти».",
                "url": url,
            }
        progress = await _wait_for_hh_auth_progress(page, before_url, before_text)
        if progress.get("status") == HH_AUTH_STEP_STALLED:
            # The URL remains /account/login across role and password screens.
            # A newly visible password/code control is sufficient progress.
            if (
                await _first_visible_locator(page, HH_AUTH_PASSWORD_INPUT_SELECTORS)
                or await _first_visible_locator(page, HH_AUTH_CODE_MODE_SELECTORS)
                or _looks_like_hh_auth_code_prompt(await _hh_auth_page_text(page), str(getattr(page, "url", "") or ""))
            ):
                progress = {"status": HH_AUTH_STEP_PROGRESS, "detail": "HH принял выбор профиля соискателя."}
        if progress.get("status") == HH_AUTH_STEP_PROGRESS:
            progress["status"] = HH_AUTH_STEP_SUBMITTED
        return progress

    if _looks_like_hh_auth_code_prompt(text, url):
        if not await capture_prompt("code"):
            return stale
        wait_s = max(1, min(int(timeout_s or 1), 900))
        code = await _request_hh_auth_value(
            "code",
            profile_name,
            "На телефон должен прийти одноразовый код для входа в hh.ru.",
            page_url=url,
            timeout_s=wait_s,
            poll_sec=poll_sec,
        )
        code = _normalize_hh_auth_code(code)
        if not code:
            return {"status": HH_AUTH_STEP_IDLE, "detail": "SMS-код не получен."}
        if not await current_prompt("code"):
            return stale
        if not await _fill_hh_auth_code(page, code, guard=lambda: credential_guard("code"),
                                        owner_guard=mutation_owner, receipts=receipts):
            return {"status": HH_AUTH_STEP_BLOCKED, "detail": "Не нашёл поле для ввода SMS-кода."}
        before_url = str(getattr(page, "url", "") or "")
        before_text = await _hh_auth_page_text(page)
        if not await current_prompt("code"):
            return stale
        if not receipts:
            return stale
        submit_result = await _submit_hh_auth_form(page, guard=lambda: credential_guard("code"), owner_guard=mutation_owner)
        if submit_result.get("status") == HH_AUTH_STEP_BLOCKED:
            return stale
        if submit_result.get("status") == HH_AUTH_STEP_CAPTCHA:
            return await _solve_hh_auth_captcha(client, profile_name, stage=f"hh_auth:{profile_name}:code_submit")
        progress = await _wait_for_hh_auth_progress(page, before_url, before_text)
        if progress.get("status") == HH_AUTH_STEP_CAPTCHA:
            progress = await _solve_hh_auth_captcha(client, profile_name, stage=f"hh_auth:{profile_name}:code_submit")
        if progress.get("status") == HH_AUTH_STEP_PROGRESS:
            progress["status"] = HH_AUTH_STEP_SUBMITTED
        return progress

    # HH may leave the old CAPTCHA node in the DOM while showing the next
    # login form. Prefer an actionable auth prompt; only then handle CAPTCHA.
    if (
        not _looks_like_hh_auth_login_prompt(text, url)
        and not await _first_visible_locator(page, HH_AUTH_LOGIN_INPUT_SELECTORS)
        and await _has_hh_auth_captcha_marker(page, text, url)
    ):
        return await _solve_hh_auth_captcha(client, profile_name, stage=f"hh_auth:{profile_name}:detect")

    if _looks_like_hh_auth_login_prompt(text, url):
        await _click_first_visible(page, HH_AUTH_PHONE_MODE_SELECTORS)
        if not await capture_prompt("login"):
            return stale
        login = auth_login
        if not login:
            wait_s = max(1, min(int(timeout_s or 1), 900))
            login = await _request_hh_auth_value(
                "login",
                profile_name,
                "Не нашёл HH_AUTH_LOGIN/HH_PHONE в env. Нужен телефон или email для запроса кода.",
                page_url=url,
                timeout_s=wait_s,
                poll_sec=poll_sec,
            )
        if login and not await current_prompt("login"):
            return stale
        if login and await _fill_hh_auth_login(page, login, guard=lambda: credential_guard("login"),
                                               owner_guard=mutation_owner, receipts=receipts):
            before_url = str(getattr(page, "url", "") or "")
            before_text = await _hh_auth_page_text(page)
            if not await current_prompt("login"):
                return stale
            if not receipts:
                return stale
            submit_result = await _submit_hh_auth_form(page, guard=lambda: credential_guard("login"), owner_guard=mutation_owner)
            if submit_result.get("status") == HH_AUTH_STEP_BLOCKED:
                return stale
            if submit_result.get("status") == HH_AUTH_STEP_CAPTCHA:
                return await _solve_hh_auth_captcha(client, profile_name, stage=f"hh_auth:{profile_name}:login_submit")
            progress = await _wait_for_hh_auth_progress(page, before_url, before_text)
            if progress.get("status") == HH_AUTH_STEP_CAPTCHA:
                progress = await _solve_hh_auth_captcha(client, profile_name, stage=f"hh_auth:{profile_name}:login_submit")
            if progress.get("status") == HH_AUTH_STEP_PROGRESS:
                progress["status"] = HH_AUTH_STEP_SUBMITTED
            return progress

    if await _first_visible_locator(page, HH_AUTH_LOGIN_INPUT_SELECTORS):
        if not await capture_prompt("login"):
            return stale
        login = auth_login
        if not login:
            wait_s = max(1, min(int(timeout_s or 1), 900))
            login = await _request_hh_auth_value(
                "login",
                profile_name,
                "На HH видна форма телефона/email, но логин не найден в env. Пришли телефон или email.",
                page_url=url,
                timeout_s=wait_s,
                poll_sec=poll_sec,
            )
        if login and not await current_prompt("login"):
            return stale
        if login and await _fill_hh_auth_login(page, login, guard=lambda: credential_guard("login"),
                                               owner_guard=mutation_owner, receipts=receipts):
            before_url = str(getattr(page, "url", "") or "")
            before_text = await _hh_auth_page_text(page)
            if not await current_prompt("login"):
                return stale
            if not receipts:
                return stale
            submit_result = await _submit_hh_auth_form(page, guard=lambda: credential_guard("login"), owner_guard=mutation_owner)
            if submit_result.get("status") == HH_AUTH_STEP_BLOCKED:
                return stale
            if submit_result.get("status") == HH_AUTH_STEP_CAPTCHA:
                return await _solve_hh_auth_captcha(client, profile_name, stage=f"hh_auth:{profile_name}:visible_login_submit")
            progress = await _wait_for_hh_auth_progress(page, before_url, before_text)
            if progress.get("status") == HH_AUTH_STEP_CAPTCHA:
                progress = await _solve_hh_auth_captcha(client, profile_name, stage=f"hh_auth:{profile_name}:visible_login_submit")
            if progress.get("status") == HH_AUTH_STEP_PROGRESS:
                progress["status"] = HH_AUTH_STEP_SUBMITTED
            return progress

    return {"status": HH_AUTH_STEP_IDLE, "detail": "На странице HH нет распознанного auth prompt.", "url": url}


async def import_current_hh_resumes(client: HHClient, profile_name: str) -> dict:
    profile = _resolve_profile(profile_name)
    cookie_paths = getattr(client, "_cookie_paths", None)
    binding = getattr(client, "_cookie_binding", None)
    cookie_revision = getattr(binding, "revision", None)
    context = getattr(client, "_context", None)
    def verify_cookie_owner(*, locked=False):
        if cookie_paths is None:
            raise RuntimeError("HH resume import requires a captured native cookie owner")
        configured = getattr(getattr(profile, "hh", None), "cookies_file", "")
        if not configured or os.path.abspath(configured) != cookie_paths.cookies_file:
            raise RuntimeError("HH import client belongs to another profile")
        if (binding is None or binding is not getattr(client, "_cookie_binding", None)
                or binding.context is not context or context is not getattr(client, "_context", None)
                or binding.revoked or binding.closing or binding.revision != cookie_revision):
            raise RuntimeError("HH import cookie ownership changed")
        snapshot = binding.repository._snapshot_unlocked if locked else binding.repository.snapshot
        if snapshot()[1] != cookie_revision:
            raise RuntimeError("HH import account/session changed")
    verify_cookie_owner()
    from hh.browser import HH_AUTH_COOKIE_NAMES
    from state_store.hh_cookies import validate_cookies
    def auth_fingerprint(cookies):
        validate_cookies(cookies)
        items = sorted((item['name'], item['value'], item.get('domain', ''), item.get('path', '/'))
                       for item in cookies if item['name'].casefold() in HH_AUTH_COOKIE_NAMES)
        if not any(item[0].casefold() == 'hhtoken' for item in items):
            raise RuntimeError("HH resume import has no captured browser auth")
        return items
    captured_auth = auth_fingerprint(binding.repository.snapshot()[0] or [])
    async def verify_browser_owner():
        verify_cookie_owner()
        current_cookies = await context.cookies()
        verify_cookie_owner()
        if auth_fingerprint(current_cookies) != captured_auth:
            raise RuntimeError("HH resume import browser auth changed; manual review required")
    workflow = HHResumeImport(profile.home_dir, profile.resume_file)
    try:
        workflow.begin()
        await verify_browser_owner()
        resumes = await client.get_resume_ids()
        await verify_browser_owner()
        exports: list[dict] = []
        seen_ids = set()
        for item in resumes:
            resume_id = str(item.get("id") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", resume_id):
                raise ValueError("Invalid HH resume export identity")
            if resume_id in seen_ids:
                raise ValueError("Ambiguous duplicate HH resume export identity")
            seen_ids.add(resume_id)
            downloaded = await client.download_resume_by_id(item)
            await verify_browser_owner()
            title = str(downloaded.get("title") or item.get("title") or resume_id)
            path = workflow.exports / f"{resume_id}.md"
            _write_text(str(path), downloaded.get("raw", ""))
            exports.append({"id": resume_id, "title": title, "url": str(item.get("url") or ""),
                            "path": str(path), "sections": downloaded.get("sections", [])})
        selected_exports = _select_profile_resumes(profile_name, exports)
        selected = selected_exports[0] if selected_exports else None
        raw = Path(selected["path"]).read_text(encoding="utf-8") if selected else ""
        await verify_browser_owner()
        def publish():
            workflow.publish(selected_exports, raw, lambda env, expected: _update_profile_resume_ids(
                profile_name, selected_exports, env_file=env, expected_content=expected))
        if binding is not None:
            # Lock only around synchronous publication, never browser downloads.
            with file_lock(binding.repository.path):
                verify_cookie_owner(locked=True)
                publish()
        else:
            publish()
    except BaseException:
        try:
            workflow.fail()
        except Exception as exc:
            log.warning("HH resume import completion needs manual review: %s", type(exc).__name__)
        raise
    return {
        "ok": bool(exports),
        "count": len(exports),
        "catalog_path": str(workflow.catalog.path),
        "profile_env_path": str(workflow.env) if selected_exports else "",
        "resume_file": str(workflow.resume) if selected_exports else "",
        "resumes": exports,
    }


async def run_hh_auth_capture(
    profile_name: str,
    *,
    timeout_sec: int = 900,
    poll_sec: int = 3,
    activate_profile: bool = True,
    import_resumes: bool = False,
) -> dict:
    target_profile = _resolve_profile(profile_name)
    auth_login = _resolve_hh_auth_login(_load_hh_auth_env(profile_name))
    if activate_profile:
        profile_mod.activate_no_lock(profile_name)
    client = HHClient()
    authenticated = False
    try:
        await client.start(headless=False)
        await _open_hh_login(  # noqa: SLF001 - auth flow owns the browser page
            client._page,
            f"{config.HH_BASE_URL}/account/login",
        )

        deadline = time.monotonic() + max(1, timeout_sec)
        stalled_submit_count = 0
        captcha_notified = False
        blocked_notified = False
        while time.monotonic() <= deadline:
            if client._page.is_closed():  # noqa: SLF001 - auth flow owns the page lifecycle
                return {
                    "ok": False,
                    "authenticated": False,
                    "timeout": False,
                    "profile_name": profile_name,
                    "cookies_file": target_profile.hh.cookies_file,
                    "count": 0,
                    "resumes": [],
                    "error": "Окно HH auth было закрыто до завершения входа.",
                }
            logged_in = await client.is_logged_in_passive()
            if (
                not logged_in
                and hasattr(client, "has_auth_cookies")
                and await client.has_auth_cookies()
                and not await _page_has_hh_auth_prompt(client._page)  # noqa: SLF001
            ):
                logged_in = await client.is_logged_in()
            if logged_in:
                await client.save_session()
                authenticated = True
                result = {
                    "ok": True,
                    "authenticated": True,
                    "timeout": False,
                    "profile_name": profile_name,
                    "cookies_file": target_profile.hh.cookies_file,
                    "imported_resumes": False,
                    "count": 0,
                    "resumes": [],
                }
                if import_resumes:
                    imported = await import_current_hh_resumes(client, profile_name)
                    result.update(imported)
                    result["ok"] = bool(imported.get("ok", False))
                    result["imported_resumes"] = True
                return result
            remaining = max(1, int(deadline - time.monotonic()))
            with contextlib.suppress(Exception):
                await client._save_debug_snapshot("debug_hh_auth_current")  # noqa: SLF001
            step = await _drive_hh_auth_step(
                client,
                profile_name,
                timeout_s=remaining,
                poll_sec=max(0.5, float(poll_sec or 1)),
                auth_login=auth_login,
            )
            status = str(step.get("status") or HH_AUTH_STEP_IDLE)
            if status in (HH_AUTH_STEP_SUBMITTED, HH_AUTH_STEP_PROGRESS):
                stalled_submit_count = 0
            elif status == HH_AUTH_STEP_STALLED:
                stalled_submit_count += 1
                if stalled_submit_count >= 3:
                    detail = str(step.get("detail") or "HH не меняет страницу после отправки формы.")
                    with contextlib.suppress(Exception):
                        await client._save_debug_snapshot("debug_hh_auth_stalled")  # noqa: SLF001
                    await _notify_hh_auth_warning(
                        profile_name,
                        "HH auth застрял на форме входа",
                        f"{detail} Бот сделал 3 попытки отправить форму без видимого прогресса. Проверь номер/почту в открытом окне HH или перезапусти вход.",
                        page_url=str(step.get("url") or getattr(client._page, "url", "") or ""),  # noqa: SLF001
                    )
                    return {
                        "ok": False,
                        "authenticated": False,
                        "timeout": False,
                        "profile_name": profile_name,
                        "cookies_file": target_profile.hh.cookies_file,
                        "count": 0,
                        "resumes": [],
                        "error": "HH auth застрял: после 3 отправок формы страница не изменилась. Проверь номер/почту или captcha в открытом браузере.",
                        "reason": "no_progress_after_submit",
                    }
            elif status == HH_AUTH_STEP_CAPTCHA:
                stalled_submit_count = 0
                if step.get("solver_attempted"):
                    return {
                        "ok": False,
                        "authenticated": False,
                        "timeout": False,
                        "profile_name": profile_name,
                        "cookies_file": target_profile.hh.cookies_file,
                        "count": 0,
                        "resumes": [],
                        "error": "HH auth остановлен: captcha не удалось снять через vision/Telegram bridge. Нажми «Повторить вход HH» из сообщения с captcha, чтобы получить свежий токен.",
                        "reason": "captcha_unsolved",
                    }
                if not captcha_notified:
                    captcha_notified = True
                    await _notify_hh_auth_warning(
                        profile_name,
                        "HH показал captcha",
                        "Captcha solver недоступен. Реши captcha вручную в открытом браузере, бот продолжит ждать вход до таймаута.",
                        page_url=str(step.get("url") or getattr(client._page, "url", "") or ""),  # noqa: SLF001
                    )
            elif status == HH_AUTH_STEP_BLOCKED:
                stalled_submit_count = 0
                if not blocked_notified:
                    blocked_notified = True
                    await _notify_hh_auth_warning(
                        profile_name,
                        "HH не принял форму входа",
                        str(step.get("detail") or "HH показал ошибку в форме входа. Проверь введённый телефон/email в открытом браузере."),
                        page_url=str(step.get("url") or getattr(client._page, "url", "") or ""),  # noqa: SLF001
                    )
            await asyncio.sleep(poll_sec)

        return {
            "ok": False,
            "authenticated": False,
            "timeout": True,
            "profile_name": profile_name,
            "cookies_file": target_profile.hh.cookies_file,
            "count": 0,
            "resumes": [],
        }
    finally:
        # A failed login page may contain a new anonymous cookie set. Do not
        # overwrite a previously working session with it. The TypeError fallback
        # keeps third-party HHClient-compatible integrations working.
        if authenticated:
            await client.stop()
        else:
            try:
                await client.stop(persist_cookies=False)
            except TypeError as exc:
                if "persist_cookies" not in str(exc):
                    raise
                await client.stop()
        with contextlib.suppress(Exception):
            import notifier

            await notifier.close_session()


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="HH auth capture for client profile")
    parser.add_argument("--profile", required=True, help="Client profile name")
    parser.add_argument("--timeout", type=int, default=900, help="HH auth timeout in seconds")
    parser.add_argument("--import-resumes", action="store_true", help="Import HH resumes after successful auth")
    args = parser.parse_args()

    try:
        result = await run_hh_auth_capture(
            args.profile,
            timeout_sec=args.timeout,
            activate_profile=True,
            import_resumes=args.import_resumes,
        )
    except Exception as exc:
        result = {
            "ok": False,
            "authenticated": False,
            "timeout": False,
            "profile_name": args.profile,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
