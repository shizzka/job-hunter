"""Telegram-уведомления о вакансиях и приглашениях."""
import logging
import aiohttp

import config

log = logging.getLogger("notifier")

_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        connector = None
        if config.TELEGRAM_PROXY:
            try:
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(config.TELEGRAM_PROXY)
            except ImportError:
                log.warning("aiohttp-socks not installed, proxy disabled")
        _session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=15),
        )
    return _session


async def send_message(text: str, parse_mode: str = "HTML"):
    """Отправить сообщение в Telegram."""
    if not config.TELEGRAM_BOT_TOKEN or not config.NOTIFY_CHAT_ID:
        log.warning("Telegram not configured (no token or chat_id)")
        return

    try:
        session = await _get_session()
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": config.NOTIFY_CHAT_ID,
            "text": text[:4096],
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                data = await resp.text()
                log.error("Telegram send failed: %s %s", resp.status, data[:200])
    except Exception as e:
        log.error("Telegram notification failed: %s", e)


async def notify_search_started(source_labels: list[str]):
    """Уведомить о старте прогона поиска."""
    if not source_labels:
        return

    sources = ", ".join(source_labels)
    text = (
        f"🚀 <b>Запуск поиска вакансий</b>\n\n"
        f"Начата процедура поиска вакансий.\n"
        f"🌐 Площадки: {sources}"
    )
    await send_message(text)


async def notify_application(vacancy: dict, score: int, cover_letter: str):
    """Уведомить об отклике на вакансию."""
    text = (
        f"📨 <b>Отклик отправлен</b>\n\n"
        f"<b>{vacancy.get('title', '—')}</b>\n"
        f"🌐 {vacancy.get('source_label', vacancy.get('source', '—'))}\n"
        f"🏢 {vacancy.get('company', '—')}\n"
        f"💰 {vacancy.get('salary', 'не указана')}\n"
        f"📊 Совпадение: {score}/100\n\n"
        f"<a href=\"{vacancy.get('url', '')}\">Открыть вакансию</a>"
    )
    if cover_letter:
        text += f"\n\n💬 <i>{cover_letter[:300]}</i>"
    await send_message(text)


async def notify_invitation(invitation: dict):
    """Уведомить о приглашении на собеседование!"""
    text = (
        f"🎉 <b>ПРИГЛАШЕНИЕ!</b>\n\n"
        f"<b>{invitation.get('title', '—')}</b>\n"
        f"🏢 {invitation.get('company', '—')}\n\n"
        f"<a href=\"{invitation.get('url', '')}\">Посмотреть</a>\n\n"
        f"⚡ Проверь hh.ru и ответь!"
    )
    await send_message(text)


async def notify_needs_manual(vacancy: dict, score: int, reason: str, note: str | None = None):
    """Уведомить о подходящей вакансии, где нужен ручной отклик."""
    extra_note = note or "Работодатель требует ответить на вопросы"
    text = (
        f"📝 <b>Подходящая вакансия (нужен ручной отклик)</b>\n\n"
        f"<b>{vacancy.get('title', '—')}</b>\n"
        f"🌐 {vacancy.get('source_label', vacancy.get('source', '—'))}\n"
        f"🏢 {vacancy.get('company', '—')}\n"
        f"💰 {vacancy.get('salary', 'не указана')}\n"
        f"📊 Совпадение: {score}/100\n\n"
        f"⚠️ {extra_note}\n"
        f"💡 {reason[:250] if reason else 'Нужно проверить вакансию вручную'}\n\n"
        f"<a href=\"{vacancy.get('url', '')}\">Открыть и откликнуться</a>"
    )
    await send_message(text)


def _format_source_stats(source_stats: dict | None) -> str:
    if not source_stats:
        return ""

    order = ("hh", "habr", "geekjob", "superjob")
    lines = []
    for source in order:
        bucket = source_stats.get(source)
        if not bucket:
            continue
        if not any(bucket.get(key, 0) for key in ("new", "relevant", "applied", "manual", "rejected")):
            continue
        parts = []
        if bucket.get("new"):
            parts.append(f"новых {bucket['new']}")
        if bucket.get("relevant"):
            parts.append(f"релевантных {bucket['relevant']}")
        if bucket.get("applied"):
            parts.append(f"к отклику {bucket['applied']}")
        if bucket.get("manual"):
            parts.append(f"ручных {bucket['manual']}")
        if bucket.get("rejected"):
            parts.append(f"отсеяно {bucket['rejected']}")
        lines.append(f"• <b>{bucket.get('label', source)}</b>: " + ", ".join(parts))

    # На случай новых источников вне явного порядка
    for source, bucket in source_stats.items():
        if source in order:
            continue
        if not any(bucket.get(key, 0) for key in ("new", "relevant", "applied", "manual", "rejected")):
            continue
        parts = []
        if bucket.get("new"):
            parts.append(f"новых {bucket['new']}")
        if bucket.get("relevant"):
            parts.append(f"релевантных {bucket['relevant']}")
        if bucket.get("applied"):
            parts.append(f"к отклику {bucket['applied']}")
        if bucket.get("manual"):
            parts.append(f"ручных {bucket['manual']}")
        if bucket.get("rejected"):
            parts.append(f"отсеяно {bucket['rejected']}")
        lines.append(f"• <b>{bucket.get('label', source)}</b>: " + ", ".join(parts))

    if not lines:
        return ""
    return "\n\n🌐 <b>По площадкам</b>\n" + "\n".join(lines)


async def notify_summary(total_found: int, applied: int, skipped: int, source_stats: dict | None = None):
    """Итог прогона поиска."""
    if total_found == 0 and applied == 0:
        return  # не спамить если ничего нового

    manual_total = sum((bucket or {}).get("manual", 0) for bucket in (source_stats or {}).values())

    text = (
        f"📋 <b>Итог поиска</b>\n\n"
        f"🔍 Релевантных вакансий: {total_found}\n"
        f"📨 К отклику: {applied}\n"
        f"📝 Ручной разбор: {manual_total}\n"
        f"⏭ Не отправлено автоматически: {skipped}"
    )
    text += _format_source_stats(source_stats)
    await send_message(text)


def _format_funnel(funnel: dict) -> str:
    if not funnel or funnel.get("applied", 0) == 0:
        return ""
    return (
        f"\n\n📊 <b>Воронка</b>\n"
        f"откликов: {funnel['applied']}\n"
        f"просмотрено: {funnel['viewed']} ({funnel['response_rate']:.1f}%)\n"
        f"ожидание: {funnel['pending']} | "
        f"отказ: {funnel['rejected']} | "
        f"позитив: {funnel['positive']} ({funnel['positive_rate']:.1f}%)"
    )


def _format_ab_resume(by_resume_variant: dict) -> str:
    if not by_resume_variant:
        return ""
    items = sorted(
        by_resume_variant.items(),
        key=lambda item: (-item[1].get("positive_rate", 0), -item[1].get("applications", 0)),
    )
    lines = []
    for variant, b in items:
        lines.append(
            f"<b>{variant}</b>: "
            f"{b.get('applications', 0)} откл, "
            f"{b.get('viewed', 0)} просм, "
            f"{b.get('positive', 0)} пос, "
            f"{b.get('rejected', 0)} отк — "
            f"resp {b.get('response_rate', 0):.0f}% conv {b.get('positive_rate', 0):.0f}%"
        )
    return "\n\n🔬 <b>A/B резюме</b>\n" + "\n".join(lines)


async def notify_digest(analytics_summary: dict):
    """Отправить полный дайджест с воронкой и A/B в Telegram."""
    if not config.TELEGRAM_BOT_TOKEN or not config.NOTIFY_CHAT_ID:
        return

    days = analytics_summary.get("days", 30)
    text = (
        f"📈 <b>Дайджест за {days} дн.</b>\n\n"
        f"решений: {analytics_summary.get('decisions', 0)} | "
        f"автооткликов: {analytics_summary.get('auto_applied', 0)} | "
        f"manual: {analytics_summary.get('manual', 0)}\n"
        f"фильтр: {analytics_summary.get('keyword_filtered', 0)} | "
        f"red flags: {analytics_summary.get('red_flagged', 0)} | "
        f"low score: {analytics_summary.get('low_score', 0)}"
    )

    funnel = analytics_summary.get("funnel", {})
    text += _format_funnel(funnel)
    text += _format_ab_resume(analytics_summary.get("by_resume_variant", {}))

    await send_message(text)


async def close_session():
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None
