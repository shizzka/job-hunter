"""HH resume refresh and boost CLI command handlers."""

import contextlib
import logging
import os
import re
import tempfile
from pathlib import Path

import config
from hh_client import HHClient
from state_store.hh_resume_import import read_optional
from state_store.json_store import file_lock


log = logging.getLogger("agent")


def _publish_refreshed_resume(path, raw, original):
    """Replace a complete fsynced file; replacement is the final commit point."""
    target = Path(path)
    with file_lock(target):
        if read_optional(target) != original:
            raise RuntimeError("Локальное резюме изменилось во время загрузки; повторите обновление.")
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw.rstrip() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(temporary)


async def do_hh_resume_refresh() -> bool:
    """Refresh only the configured HH ID, without selecting or changing settings."""
    resume_id = str(config.HH_PRIMARY_RESUME_ID or "").strip()
    configured_title = config.HH_PRIMARY_RESUME_TITLE
    resume_path = config.RESUME_FILE
    try:
        if not resume_id:
            raise ValueError("Сначала выберите резюме HH в «Мой поиск» → «Выбрать резюме».")
        if re.fullmatch(r"[A-Za-z0-9_-]+", resume_id) is None:
            raise ValueError("Некорректный ID выбранного резюме HH.")
        original = read_optional(resume_path)
        client = HHClient()
        try:
            await client.start()
            resumes = await client.get_resume_ids_readonly()
            matches = [item for item in resumes if item.get("id") == resume_id]
            if len(matches) != 1:
                raise ValueError(f"Выбранное резюме с ID {resume_id} не найдено однозначно на HH. Проверьте ID и вход HH.")
            result = await client.download_resume_readonly(matches[0])
            if (not isinstance(result, dict) or result.get("ok") is False
                    or not isinstance(result.get("raw"), str) or not result["raw"].strip()
                    or not isinstance(result.get("title"), str) or not result["title"].strip()):
                raise ValueError("HH вернул пустое резюме или загрузка не удалась.")
        finally:
            if await client.stop(persist_cookies=False) is False:
                raise RuntimeError("Не удалось завершить сессию скачивания HH.")
        if (str(config.HH_PRIMARY_RESUME_ID or "").strip() != resume_id
                or config.HH_PRIMARY_RESUME_TITLE != configured_title or config.RESUME_FILE != resume_path):
            raise RuntimeError("Выбранное резюме изменилось во время загрузки; повторите обновление.")
        _publish_refreshed_resume(resume_path, result["raw"], original)
    except Exception as exc:
        log.warning("HH resume refresh failed: kind=%s stage=%s reason=%s", type(exc).__name__,
                    getattr(exc, "stage", "resume_refresh"), getattr(exc, "hh_stop_reason", ""))
        print(f"❌ Резюме не обновлено: {exc}\nПрежний локальный файл сохранён.")
        return False
    print(f"✅ Резюме обновлено из HH\nНазвание: {result['title']}\nID: {resume_id}")
    return True


def _print_hh_resume_boost_detail(detail: dict) -> None:
    print("📌 HH resume boost:")
    print(f"  OK: {detail.get('ok')}")
    print(f"  Резюме: {detail.get('title') or '-'} ({detail.get('resume_id') or '-'})")
    print(f"  Можно поднять: {'да' if detail.get('can_boost') else 'нет'}")
    print(f"  Причина: {detail.get('reason') or '-'}")
    if detail.get("button_text"):
        print(f"  Кнопка: {detail.get('button_text')}")
    print(f"  Найдено резюме: {detail.get('resumes_found', 0)}")
    if detail.get("debug_screenshot"):
        print(f"  Скрин: {detail.get('debug_screenshot')}")
    if detail.get("debug_html"):
        print(f"  HTML: {detail.get('debug_html')}")


async def do_hh_resume_boost_status() -> None:
    """Проверить кнопку поднятия HH-резюме без клика."""
    client = HHClient()
    try:
        await client.start()
        if not await client.is_logged_in():
            print("❌ Не залогинен! Сначала: ./run.sh login")
            return
        detail = await client.get_resume_boost_status(
            resume_id=config.HH_PRIMARY_RESUME_ID,
            resume_title=config.HH_PRIMARY_RESUME_TITLE,
        )
        _print_hh_resume_boost_detail(detail)
    except Exception as exc:
        log.error("HH resume boost status failed: %s", exc, exc_info=True)
        print(f"❌ Ошибка: {exc}")
    finally:
        await client.stop()


async def do_hh_resume_boost(confirm: str) -> None:
    """Ручное поднятие HH-резюме с явным подтверждением."""
    if not config.HH_RESUME_BOOST_ENABLED:
        print("❌ Поднятие отключено: выставь HH_RESUME_BOOST_ENABLED=1 для ручного запуска.")
        return
    if confirm != config.HH_RESUME_BOOST_CONFIRM_TEXT:
        print(f"❌ Для клика нужно подтверждение: ./run.sh resume-boost {config.HH_RESUME_BOOST_CONFIRM_TEXT}")
        return

    client = HHClient()
    try:
        await client.start()
        if not await client.is_logged_in():
            print("❌ Не залогинен! Сначала: ./run.sh login")
            return
        detail = await client.boost_resume(
            resume_id=config.HH_PRIMARY_RESUME_ID,
            resume_title=config.HH_PRIMARY_RESUME_TITLE,
            confirm=confirm,
        )
        _print_hh_resume_boost_detail(detail)
    except Exception as exc:
        log.error("HH resume boost failed: %s", exc, exc_info=True)
        print(f"❌ Ошибка: {exc}")
    finally:
        await client.stop()
