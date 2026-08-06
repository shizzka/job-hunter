"""HH resume boost CLI command handlers."""

import logging

import config
from hh_client import HHClient


log = logging.getLogger("agent")


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
