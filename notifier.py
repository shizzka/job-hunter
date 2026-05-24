"""Telegram-уведомления о вакансиях и приглашениях."""
import json
import logging
import os
import time
import aiohttp

import config
import profile as profile_mod
import telegram_access

log = logging.getLogger("notifier")

_session: aiohttp.ClientSession | None = None
_session_uses_proxy = False
_session_proxy_url = ""


def _build_connector(use_proxy: bool, proxy_url: str) -> tuple[aiohttp.BaseConnector | None, bool]:
    if use_proxy and proxy_url:
        try:
            from aiohttp_socks import ProxyConnector
            return ProxyConnector.from_url(proxy_url), True
        except ImportError:
            log.warning("aiohttp-socks not installed, proxy disabled")
    return None, False


def _active_profile():
    try:
        return profile_mod.active()
    except Exception:
        return None


def _resolve_target_chat_ids(profile) -> list[int]:
    profile_name = str(getattr(profile, "name", "") or "").strip()
    if profile_name:
        matched = []
        for item in telegram_access.list_users():
            try:
                user_id = int(item.get("user_id") or 0)
            except (TypeError, ValueError):
                continue
            if user_id <= 0 or not item.get("enabled", True):
                continue
            if str(item.get("profile") or "").strip() == profile_name:
                matched.append(user_id)
        if matched:
            return sorted(set(matched))

    configured_chat_id = 0
    try:
        configured_chat_id = int(getattr(getattr(profile, "notify", None), "chat_id", 0) or 0)
    except (TypeError, ValueError):
        configured_chat_id = 0
    if configured_chat_id > 0:
        return [configured_chat_id]

    fallback_chat_id = int(config.NOTIFY_CHAT_ID or 0)
    return [fallback_chat_id] if fallback_chat_id > 0 else []


def _resolve_bot_token(profile) -> str:
    token = str(getattr(getattr(profile, "notify", None), "bot_token", "") or "").strip()
    if token:
        return token
    token = str(config.TELEGRAM_BOT_TOKEN or "").strip()
    if token:
        return token
    return str(getattr(config, "TELEGRAM_CONTROL_BOT_TOKEN", "") or "").strip()


def _resolve_proxy_url(profile) -> str:
    proxy_url = str(getattr(getattr(profile, "notify", None), "proxy", "") or "").strip()
    if proxy_url:
        return proxy_url
    return str(config.TELEGRAM_PROXY or "").strip()


async def _get_session(use_proxy: bool = True, proxy_url: str = "") -> aiohttp.ClientSession:
    global _session, _session_uses_proxy, _session_proxy_url
    connector, session_uses_proxy = _build_connector(use_proxy, proxy_url)
    expected_proxy_url = proxy_url if session_uses_proxy else ""
    if _session and not _session.closed:
        if _session_uses_proxy == session_uses_proxy and _session_proxy_url == expected_proxy_url:
            return _session
        await _session.close()
    _session = aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=15),
    )
    _session_uses_proxy = session_uses_proxy
    _session_proxy_url = expected_proxy_url
    return _session


async def _deliver(url: str, payload: dict, use_proxy: bool, proxy_url: str) -> bool:
    """Прямой POST с JSON-телом. Используется для sendMessage и т.п."""
    session = await _get_session(use_proxy=use_proxy, proxy_url=proxy_url)
    async with session.post(url, json=payload) as resp:
        if resp.status != 200:
            data = await resp.text()
            log.error("Telegram send failed: %s %s", resp.status, data[:200])
            return False
    return True


async def _deliver_multipart(url: str, form: "aiohttp.FormData", use_proxy: bool, proxy_url: str) -> bool:
    """POST с multipart/form-data — для sendPhoto/sendDocument."""
    session = await _get_session(use_proxy=use_proxy, proxy_url=proxy_url)
    async with session.post(url, data=form) as resp:
        if resp.status != 200:
            data = await resp.text()
            log.error("Telegram multipart send failed: %s %s", resp.status, data[:200])
            return False
    return True


async def _send_to_chats(method: str, build_request, *, multipart: bool = False) -> bool:
    """Универсальная отправка в Telegram-API с резолвом профиля/токена/чатов и
    автоматическим proxy→direct retry.

    ``build_request(chat_id)`` возвращает либо dict (json-payload) либо
    ``aiohttp.FormData`` (multipart). Вызывается заново для каждого chat_id и для
    каждой попытки (proxy/direct), потому что file-handle в FormData одноразовый.
    """
    profile = _active_profile()
    bot_token = _resolve_bot_token(profile)
    chat_ids = _resolve_target_chat_ids(profile)
    if not bot_token or not chat_ids:
        log.warning("Telegram not configured (no token or targets)")
        return False

    proxy_url = _resolve_proxy_url(profile)
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    proxy_modes = [(True, proxy_url)] if proxy_url else []
    proxy_modes.append((False, ""))  # direct fallback всегда есть

    delivered_any = False
    for chat_id in chat_ids:
        for use_proxy, proxy_arg in proxy_modes:
            try:
                payload = build_request(chat_id)
                if multipart:
                    ok = await _deliver_multipart(url, payload, use_proxy=use_proxy, proxy_url=proxy_arg)
                else:
                    ok = await _deliver(url, payload, use_proxy=use_proxy, proxy_url=proxy_arg)
            except Exception as e:
                log.warning("Telegram %s error (use_proxy=%s) chat=%s: %s", method, use_proxy, chat_id, e)
                continue
            if ok:
                delivered_any = True
                break  # success — следующий чат
    return delivered_any


def _build_text_payload(text: str, parse_mode: str, reply_markup: dict | None):
    """Фабрика payload-словаря для sendMessage."""
    def build(chat_id):
        payload = {
            "chat_id": chat_id,
            "text": text[:4096],
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return payload
    return build


def _build_photo_form(photo_path: str, caption: str, parse_mode: str, reply_markup: dict | None):
    """Фабрика FormData для sendPhoto. Открывает файл заново на каждый вызов."""
    def build(chat_id):
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if caption:
            form.add_field("caption", caption[:1024])
            form.add_field("parse_mode", parse_mode)
        if reply_markup:
            form.add_field("reply_markup", json.dumps(reply_markup, ensure_ascii=False))
        form.add_field(
            "photo",
            open(photo_path, "rb"),
            filename=os.path.basename(photo_path),
            content_type="image/png",
        )
        return form
    return build


async def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Простое текстовое уведомление."""
    return await _send_to_chats("sendMessage", _build_text_payload(text, parse_mode, None))


async def send_message_with_markup(text: str, reply_markup: dict | None = None, parse_mode: str = "HTML") -> bool:
    """Текстовое сообщение с inline-клавиатурой."""
    return await _send_to_chats("sendMessage", _build_text_payload(text, parse_mode, reply_markup))


async def send_photo(photo_path: str, caption: str = "", parse_mode: str = "HTML", reply_markup: dict | None = None) -> bool:
    """Отправить фото с опциональной подписью и клавиатурой."""
    if not os.path.exists(photo_path):
        log.warning("send_photo: file not found %s", photo_path)
        return False
    return await _send_to_chats(
        "sendPhoto",
        _build_photo_form(photo_path, caption, parse_mode, reply_markup),
        multipart=True,
    )


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


def _format_autoanswer_notes(note: str) -> str:
    """Красиво форматирует строку с записями автоответов на анкету hh.ru.

    Принимает note в формате 'автоответ hh: вопрос -> ответ; автоответ hh (radio): ...'
    Возвращает multiline блок с эмодзи-метками по типу контрола.

    Если note не похож на список автоответов — возвращает его без изменений.
    """
    if not note or "автоответ hh" not in note:
        return note[:600]
    items = [item.strip() for item in note.split(";") if item.strip()]
    if not items:
        return note[:600]
    lines = []
    for item in items:
        # эмодзи по типу
        icon = "📝"
        if "(radio)" in item:
            icon = "🔘"
        elif "(checkbox)" in item:
            icon = "☑️"
        elif "(select)" in item:
            icon = "▾"
        elif "зарплатные ожидания" in item.lower() or "зарплат" in item.lower():
            icon = "💰"
        if "[best-guess]" in item:
            icon += "🎯"
        # убираем технический префикс «автоответ hh [(control)] [best-guess]:» —
        # иконка уже несёт эту инфу.
        import re
        clean = re.sub(
            r"^автоответ\s+hh\s*(\([a-zA-Z]+\))?\s*(\[best-guess\])?\s*:\s*",
            "",
            item,
        ).strip()
        # обрезаем длинные строки до 220 символов
        if len(clean) > 220:
            clean = clean[:217] + "…"
        lines.append(f"  {icon} {clean}")
    block = "\n".join(lines)
    if len(block) > 1500:
        block = block[:1497] + "…"
    return block


async def notify_application(
    vacancy: dict,
    score: int,
    cover_letter: str,
    note: str | None = None,
):
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
    if note:
        formatted = _format_autoanswer_notes(note)
        # если форматтер вернул multiline — даём отдельный заголовок «Анкета»
        if "\n" in formatted:
            text += f"\n\n🧠 <b>Анкета (auto-answer):</b>\n{formatted}"
        else:
            text += f"\n\n🧠 {formatted}"
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
            f"ответ {b.get('response_rate', 0):.0f}% успех {b.get('positive_rate', 0):.0f}%"
        )
    return "\n\n🔬 <b>A/B резюме</b>\n" + "\n".join(lines)


async def notify_digest(analytics_summary: dict):
    """Отправить полный дайджест с воронкой и A/B в Telegram."""
    if not _resolve_bot_token(_active_profile()) or not _resolve_target_chat_ids(_active_profile()):
        return

    days = analytics_summary.get("days", 30)
    text = (
        f"📈 <b>Дайджест за {days} дн.</b>\n\n"
        f"решений: {analytics_summary.get('decisions', 0)} | "
        f"автооткликов: {analytics_summary.get('auto_applied', 0)} | "
        f"ручных: {analytics_summary.get('manual', 0)}\n"
        f"фильтр: {analytics_summary.get('keyword_filtered', 0)} | "
        f"красных флагов: {analytics_summary.get('red_flagged', 0)} | "
        f"низкий балл: {analytics_summary.get('low_score', 0)}"
    )

    funnel = analytics_summary.get("funnel", {})
    text += _format_funnel(funnel)
    text += _format_ab_resume(analytics_summary.get("by_resume_variant", {}))

    await send_message(text)


_COOKIE_WARN_FILE = os.path.join(os.path.expanduser("~"), ".job-hunter", "cookie_warn_sent.json")
_COOKIE_WARN_INTERVAL = 24 * 3600  # раз в сутки
_COOKIE_STALE_DAYS = 7


async def notify_stale_cookies():
    """Отправить уведомление если куки площадок устарели (>7 дней)."""
    sources = {
        "hh.ru": config.HH_COOKIES_FILE,
        "SuperJob": config.SUPERJOB_COOKIES_FILE,
        "Habr Career": config.HABR_COOKIES_FILE,
        "GeekJob": config.GEEKJOB_COOKIES_FILE,
    }
    now = time.time()
    stale = []
    for name, path in sources.items():
        if not os.path.exists(path):
            stale.append(f"❌ {name}: файл куки не найден")
            continue
        age_days = (now - os.path.getmtime(path)) / 86400
        if age_days >= _COOKIE_STALE_DAYS:
            stale.append(f"⚠️ {name}: {age_days:.0f} дн. без обновления")

    if not stale:
        return

    # Не спамить чаще раза в сутки
    try:
        state = json.loads(open(_COOKIE_WARN_FILE).read()) if os.path.exists(_COOKIE_WARN_FILE) else {}
    except Exception:
        state = {}
    if now - state.get("sent_at", 0) < _COOKIE_WARN_INTERVAL:
        return

    text = (
        "🍪 <b>Куки площадок устарели</b>\n\n"
        + "\n".join(stale)
        + "\n\n<b>Обновить:</b>\n"
        "<code>./run.sh login</code>\n"
        "<code>./run.sh habr-login</code>\n"
        "<code>./run.sh superjob-login</code>\n"
        "<code>./run.sh geekjob-login</code>"
    )
    await send_message(text)

    try:
        with open(_COOKIE_WARN_FILE, "w") as f:
            json.dump({"sent_at": now, "stale": stale}, f)
    except Exception:
        pass


async def close_session():
    global _session, _session_uses_proxy, _session_proxy_url
    if _session and not _session.closed:
        await _session.close()
        _session = None
    _session_uses_proxy = False
    _session_proxy_url = ""
