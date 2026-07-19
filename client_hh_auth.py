"""HH auth and resume import flow for client profiles."""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

import config
import profile as profile_mod
from hh_client import HHClient


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
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", (value or "").strip()).strip("_")
    return slug or "resume"


def _write_text(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n")
    return path


def _normalize_env_value(value: str | int | None) -> str:
    raw = "" if value is None else str(value)
    return re.sub(r"\s+", " ", raw).strip()


def _save_resume_catalog(profile_name: str, items: list[dict]) -> str:
    path = hh_resume_catalog_path(profile_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
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


def _update_profile_resume_ids(profile_name: str, resumes: list[dict]) -> str:
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

    with open(env_file, encoding="utf-8") as f:
        lines = f.read().splitlines()

    key_to_index: dict[str, int] = {}
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, _ = stripped.partition("=")
        key_to_index[key.strip()] = idx

    for key, value in updates.items():
        rendered = f"{key}={_normalize_env_value(value)}"
        if key in key_to_index:
            lines[key_to_index[key]] = rendered
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(rendered)

    with open(env_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")

    return env_file



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
    "input[autocomplete='username']",
    "input[data-qa*='login' i]",
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
    "button:has-text('Продолжить')",
    "button:has-text('Получить код')",
    "button:has-text('Выслать код')",
    "button:has-text('Отправить код')",
    "button:has-text('Войти')",
    "button:has-text('Подтвердить')",
    "button[type='submit']",
    "input[type='submit']",
)

HH_AUTH_PHONE_MODE_SELECTORS = (
    "button:has-text('Телефон')",
    "a:has-text('Телефон')",
    "button:has-text('по телефону')",
    "a:has-text('по телефону')",
    "button:has-text('по коду')",
    "a:has-text('по коду')",
)


def _resolve_hh_auth_login() -> str:
    for key in HH_AUTH_LOGIN_ENV_KEYS:
        value = os.getenv(key, "").strip()
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
    return "войти" in haystack and any(token in haystack for token in ("телефон", "почт", "email", "логин"))


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


async def _fill_first_visible(page, selectors: tuple[str, ...], value: str) -> bool:
    item = await _first_visible_locator(page, selectors)
    if not item:
        return False
    try:
        await item.fill(value, timeout=5000)
    except TypeError:
        await item.fill(value)
    return True


async def _click_first_visible(page, selectors: tuple[str, ...]) -> bool:
    item = await _first_visible_locator(page, selectors)
    if not item:
        return False
    try:
        await item.click(timeout=5000)
    except TypeError:
        await item.click()
    return True


async def _submit_hh_auth_form(page) -> None:
    clicked = await _click_first_visible(page, HH_AUTH_CONTINUE_SELECTORS)
    if clicked:
        return
    try:
        await page.keyboard.press("Enter")
    except Exception:
        pass


async def _fill_hh_auth_code(page, code: str) -> bool:
    digits = _normalize_hh_auth_code(code)
    if not digits:
        return False
    if await _fill_first_visible(page, HH_AUTH_CODE_INPUT_SELECTORS[:6], digits):
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
                await item.fill(digit)
            return True
    except Exception:
        pass
    return await _fill_first_visible(page, HH_AUTH_CODE_INPUT_SELECTORS, digits)


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
    await _notify_hh_auth_request(kind, profile_name, prompt, timeout_s)
    try:
        answer = await hh_auth_bridge.wait_for_response(request_id, timeout_s=timeout_s, poll_interval_s=poll_sec)
        return (answer or "").strip()
    finally:
        hh_auth_bridge.complete_request(request_id)


async def _drive_hh_auth_step(client: HHClient, profile_name: str, *, timeout_s: int, poll_sec: float) -> bool:
    page = client._page  # noqa: SLF001 - auth flow owns the page lifecycle
    if page is None or page.is_closed():
        return False

    url = str(getattr(page, "url", "") or "")
    text = await _hh_auth_page_text(page)

    if _looks_like_hh_auth_code_prompt(text, url):
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
            return False
        if not await _fill_hh_auth_code(page, code):
            return False
        await _submit_hh_auth_form(page)
        await page.wait_for_timeout(2500)
        return True

    if _looks_like_hh_auth_login_prompt(text, url):
        await _click_first_visible(page, HH_AUTH_PHONE_MODE_SELECTORS)
        login = _resolve_hh_auth_login()
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
        if login and await _fill_first_visible(page, HH_AUTH_LOGIN_INPUT_SELECTORS, login):
            await _submit_hh_auth_form(page)
            await page.wait_for_timeout(2500)
            return True

        # First HH screen may only ask for account type (applicant/employer)
        # and show a submit button. Move forward to reveal phone/email fields.
        if "/account/login" in url.casefold() and await _click_first_visible(page, HH_AUTH_CONTINUE_SELECTORS):
            await page.wait_for_timeout(2500)
            return True

    return False

async def import_current_hh_resumes(client: HHClient, profile_name: str) -> dict:
    profile = _resolve_profile(profile_name)
    resumes = await client.get_resume_ids()
    exports: list[dict] = []
    exports_dir = hh_resume_exports_dir(profile_name)
    os.makedirs(exports_dir, exist_ok=True)

    for item in resumes:
        downloaded = await client.download_resume_by_id(item)
        resume_id = str(item.get("id") or "")
        title = str(downloaded.get("title") or item.get("title") or resume_id or "resume")
        filename = f"{resume_id or _slugify(title)}.md"
        path = os.path.join(exports_dir, filename)
        _write_text(path, downloaded.get("raw", ""))
        exports.append(
            {
                "id": resume_id,
                "title": title,
                "url": str(item.get("url") or ""),
                "path": path,
                "sections": downloaded.get("sections", []),
            }
        )

    selected_exports = _select_profile_resumes(profile_name, exports)
    catalog_path = _save_resume_catalog(profile_name, selected_exports)
    profile_env_path = _update_profile_resume_ids(profile_name, selected_exports) if selected_exports else ""

    selected = selected_exports[0] if selected_exports else None
    resume_file = ""
    if selected and selected.get("path"):
        raw = Path(selected["path"]).read_text(encoding="utf-8")
        resume_file = _write_text(profile.resume_file, raw)

    return {
        "ok": bool(exports),
        "count": len(exports),
        "catalog_path": catalog_path,
        "profile_env_path": profile_env_path,
        "resume_file": resume_file,
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
    if activate_profile:
        profile_mod.activate_no_lock(profile_name)
    client = HHClient()
    try:
        await client.start(headless=False)
        await client._page.goto(  # noqa: SLF001 - reusing existing HH client page flow
            f"{config.HH_BASE_URL}/account/login",
            wait_until="domcontentloaded",
            timeout=60000,
        )

        deadline = time.monotonic() + max(1, timeout_sec)
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
            if not logged_in and hasattr(client, "has_auth_cookies") and await client.has_auth_cookies():
                logged_in = await client.is_logged_in()
            if logged_in:
                await client.save_session()
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
            await _drive_hh_auth_step(
                client,
                profile_name,
                timeout_s=remaining,
                poll_sec=max(0.5, float(poll_sec or 1)),
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
        await client.stop()


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
