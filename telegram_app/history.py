"""Direction-filtered read-only history screens and their bound navigation."""
import asyncio
import hashlib
import logging
import html
import re
from pathlib import Path

import analytics
import analytics_read_cache as cache
import profile_access
from telegram_app.guards import read_thread
from direction_history import ALL, UNSPECIFIED, LEGACY_TITLE, HistoryScope
from state_store.private_journal import read_json_records

RUN_SUMMARY_TIMEOUT_SECONDS = 10.0
APPLICATION_FILTERS = {
    "/applications": ("all", "📨 Все отклики"),
    "/applications_invitations": ("positive", "📬 Приглашения"),
    "/applications_rejections": ("rejected", "❌ Отказы"),
}
APPLICATION_PAGE_SIZE = 5
EMPTY_SEARCH_LABEL = "❓ Почему ничего не нашлось"

COMMANDS = {"/stats", "/digest", "/research", "/runs", "/log", "/hunter_log", "/chat_log", "/hunter_chat_log"}


def token(home):
    view = HistoryScope(home)
    return hashlib.sha256((str(Path(home).resolve()) + "|" + view.name).encode()).hexdigest()[:12]


def profile_picker_token(home, profiles):
    """Also bind profile indexes to their displayed order."""
    return hashlib.sha256((token(home) + "|" + "|".join(profiles)).encode()).hexdigest()[:12]


def empty_search_token(run_id):
    return hashlib.sha256(str(run_id).encode()).hexdigest()[:12]


def is_empty_finished_search(run):
    """An exact suitable zero is independent of the number of sent applications."""
    if not run or not run.get("run_id") or run.get("status") in {"incomplete", "running", "cancelled"}:
        return False
    if not (run.get("finished_at") or run.get("status") == "finished"
            or run.get("execution_status") == "completed"):
        return False
    if run.get("counter_status") == "minimum_or_unknown":
        return False
    counters = run.get("counters") or run.get("funnel") or run
    if ((counters.get("applied") or 0) > 0 or (counters.get("uncertain") or 0) > 0
            or (counters.get("manual") or run.get("manual") or 0) > 0
            or (run.get("reason_breakdown") or {}).get("manual_required", 0) > 0):
        return False
    suitable = counters.get("matcher_pass")
    return type(suitable) is int and suitable == 0


def search_summary(profile, profile_name, run_id=None, *, scope="active"):
    """Retain saved manual decisions omitted by the general display summary."""
    summary = analytics.get_run_summary(run_id, events_file=profile.analytics_events_file,
        history_file=profile.run_history_file, profile=profile_name, scope=scope)
    if summary:
        try:
            records = read_json_records(profile.run_history_file)
        except FileNotFoundError:
            records = []
        record = next((r for r in reversed(records) if r.get("run_id") == summary.get("run_id")), {})
        summary = {**summary, "reason_breakdown": record.get("reason_breakdown") or {},
                   "manual": (record.get("funnel") or {}).get("manual", record.get("manual", 0))}
        if is_empty_finished_search(summary):
            # Older yellow cards were not counted in matcher_pass. Their saved
            # decision remains positive evidence even without a run checkpoint.
            manual = any(e.get("run_id") == summary["run_id"] and e.get("event") == "decision"
                and (str(e.get("decision") or "").startswith("manual_")
                     or e.get("decision") == "questions_required")
                for e in analytics.history_events(profile.analytics_events_file, scope=scope))
            if manual:
                summary["manual"] = 1
    return summary


def empty_search_markup(home, run):
    if not is_empty_finished_search(run):
        return None
    return {"inline_keyboard": [[{"text": EMPTY_SEARCH_LABEL,
        "callback_data": f"nav:{token(home)}:why_no_apply:{empty_search_token(run['run_id'])}"}]]}


def empty_search_diagnosis(run):
    """Explain only recorded search evidence, without treating unsent as unsuitable."""
    if not is_empty_finished_search(run):
        return ""
    counters = run.get("counters") or run.get("funnel") or run
    lines = [f"❓ Почему ничего не нашлось · запуск {run['run_id']}", "Подходящих вакансий: 0."]
    for key, label in (("fetched", "Получено"), ("already_seen", "Уже просмотрено"),
                       ("new", "Новых"), ("keyword_pass", "Прошло фильтры поиска"),
                       ("evaluated", "Оценено"), ("deferred_unscored", "Оценка отложена"),
                       ("not_processed", "Не обработано")):
        value = counters.get(key)
        if type(value) is int:
            lines.append(f"• {label}: {value}")
    labels = {"keyword_filter": "Фильтры поиска", "low_score": "Низкая оценка соответствия",
              "red_flags": "Стоп-факторы", "company_blacklisted": "Компания в исключениях"}
    reasons = run.get("reason_breakdown") or {}
    for key, label in labels.items():
        if type(reasons.get(key)) is int and reasons[key] > 0:
            lines.append(f"• {label}: {reasons[key]}")
    if counters.get("fetched") == 0:
        lines.append("Источники не вернули вакансий в этом поиске.")
    elif counters.get("new") == 0:
        lines.append("Новых вакансий для оценки не осталось.")
    elif counters.get("evaluated", 0):
        lines.append("Среди оценённых вакансий подходящих не оказалось.")
    else:
        lines.append("Точная причина отсутствия подходящих вакансий не записана.")
    if run.get("stage_failures") or run.get("primary_failure"):
        lines.append("Во время поиска были ошибки; результат может быть неполным. Подробнее: /log.")
    return html.escape("\n".join(lines))


def markup(home, command, *, scope="active"):
    view = HistoryScope(home)
    selected = {"*": "all", "?": "unspecified"}.get(scope, "active")
    labels = ((f"Только «{view.title}»", "active"), ("Все направления", "all"), (LEGACY_TITLE, "unspecified"))
    return {"inline_keyboard": [[{"text": ("✓ " if key == selected else "") + label,
            "callback_data": f"hist:{token(home)}:{command.removeprefix('/')}:{key}"}]
            for label, key in labels
            if not profile_access.current_scope()[1] or key == "active"]}


def scope_arg(arg):
    return {"all": ALL, "unspecified": UNSPECIFIED}.get(arg.strip(), "active")


def chat_rows(home, legacy_text=""):
    """Journal records bind via B22; old text lines need an explicit chat id."""
    import chat_context
    try:
        rows = read_json_records(Path(home) / "chat_journal.jsonl")
    except FileNotFoundError:
        rows = []
    result = []
    for row in rows:
        if not row.get("chat_id") or row.get("event") not in {"chat_decision", "chat_reply_attempt"}:
            continue
        if not row.get("direction"):
            owner = chat_context.safe_resolve(str(home), str(row["chat_id"]))
            row = {**row, "direction": owner.direction if owner.status == chat_context.OWN else None,
                   "hh_resume_numeric_id": owner.resume_id}
        result.append(row)
    # Text logs predate the journal. A chat id is the only supported bridge to
    # B22 ownership; never attribute a whole process log to its active direction.
    if not result and not profile_access.current_scope()[1]:
        for line in legacy_text.splitlines():
            match = re.search(r"\bchat(?:_id)?[=: ]+(\d+)\b", line)
            owner = chat_context.safe_resolve(str(home), match[1]) if match else None
            result.append({"legacy_text": line, "chat_id": match[1] if match else "",
                           "direction": owner.direction if owner and owner.status == chat_context.OWN else None,
                           "hh_resume_numeric_id": owner.resume_id if owner else None})
    return result


def chat_text(home, view, legacy_text=""):
    labels = {"filtered": "пропущен", "waiting": "ожидает ответа", "rejected": "отказ",
              "rejection": "отказ", "replied": "ответ отправлен", "answered": "ответ отправлен",
              "hr_question": "вопрос работодателя", "manual_review": "нужно ваше решение",
              "google_form": "анкета", "external_invite": "приглашение"}
    rows = view.records(chat_rows(home, legacy_text))[-40:]
    lines = ["💬 Лог чатов · " + view.title]
    for row in rows:
        if "legacy_text" in row:
            lines.append(row["legacy_text"])
            continue
        owner = view.owner(row)
        title = next((d["title"] for d in view.directions if d["name"] == owner), "направление не указано")
        lines.append(f"{str(row.get('observed_at') or '')[:16]} · {title} · чат {row['chat_id']}: "
                     + labels.get(row.get("decision"), "обработка чата"))
    if not rows:
        lines.append("Записей пока нет. Старые записи без сведений о направлении доступны отдельно.")
    return html.escape("\n".join(lines))


def _run_report(bot, principal, profile_name, command, arg):
    """Build a read-only report and its controls under the same error guard."""
    from telegram_bot_ui import build_run_summary_text
    profile = bot._profile(profile_name)
    scope = scope_arg(arg)
    view = HistoryScope(profile.home_dir, scope)
    selected_id = None if scope != "active" or arg == "active" else arg or None
    summary = search_summary(profile, profile_name, selected_id, scope=scope)
    if summary is None:
        text = ("Запуск не найден. Посмотрите последний запуск: /log." if selected_id
                else "Запусков пока не было.")
        return text, None, None
    visible = summary if principal.get("role") == "admin" else {**summary, "artifacts": []}
    text = f"📊 Последний запуск · {view.title}\n" + build_run_summary_text(
        visible, profile_name=profile_name, role=principal.get("role", "user"))
    if scope == ALL:
        text = f"Направление: {summary['direction_title']}\n" + text
    controls = markup(profile.home_dir, command, scope=scope)
    # A diagnosis always belongs to the active direction; all-direction and
    # legacy views must not manufacture an active-direction callback binding.
    empty_controls = empty_search_markup(profile.home_dir, summary) if scope == "active" else None
    if empty_controls:
        controls["inline_keyboard"].extend(empty_controls["inline_keyboard"])
    return text, view.title, controls


async def show_run(bot, chat_id, principal, profile_name, command, arg):
    try:
        # Leave time to send an answer before the foreground supervisor's deadline.
        # The worker only builds data: a timed-out read can never send a late reply.
        async with asyncio.timeout(RUN_SUMMARY_TIMEOUT_SECONDS):
            text, title, controls = await read_thread(_run_report, bot, principal, profile_name, command, arg)
            # Bot state and command pruning belong to the event-loop thread.
            menu = bot._menu_reply_markup(principal)
            messages = [(text, menu)] if title is None else [(f"История запусков · {title}", menu), (text, controls)]
    except TimeoutError:
        error = "Сводка готовится слишком долго. Повторите /log чуть позже; если не поможет, сообщите администратору."
    except PermissionError:
        # Preserve a denied read without exposing filesystem exception paths.
        error = "🔒 Не удалось прочитать сводку запуска: нет доступа к данным. Повторите /log в доступном направлении или сообщите администратору."
    except Exception as exc:
        logging.getLogger(__name__).warning("Run summary unavailable: %s", type(exc).__name__)
        error = "Не удалось прочитать сводку запуска. Повторите /log чуть позже; если не поможет, сообщите администратору."
    else:
        for text, reply_markup in messages:
            await bot._send_text(chat_id, text, reply_markup=reply_markup)
        return
    # Storage or access failure may also break the profile-dependent keyboard.
    await bot._send_text(chat_id, error)


def _history_report(bot, principal, profile_name, command, arg):
    from telegram_bot_ui import build_stats_text, build_runs_text
    profile = bot._profile(profile_name)
    scope = scope_arg(arg)
    view = HistoryScope(profile.home_dir, scope)
    title = html.escape(view.title)
    texts = []
    if command == "/stats":
        snapshot = bot._stats_snapshot(profile_name, scope=scope)
        snapshots = None
        if principal.get("role") == "admin":
            # one block per profile, each counted only from its own files (owner's overview, 24.10)
            names = [profile_name, *(name for name in bot._profile_names() if name != profile_name)]
            snapshots = [{**(snapshot if name == profile_name else bot._stats_snapshot(name)),
                          "selected": name == profile_name} for name in names]
        texts = [build_stats_text(**snapshot, selected=True, profile_snapshots=snapshots)]
    elif command == "/digest":
        from notifier import build_digest_text
        texts = [build_digest_text(analytics.summarize(events_file=profile.analytics_events_file, scope=scope))]
    elif command == "/research":
        import hiring_research
        texts = list(hiring_research.render(hiring_research.summarize(profile.analytics_events_file, scope=scope)))
    elif command == "/runs":
        import application_history
        runs = bot._recent_runs(profile_name, limit=5, scope=scope)
        applications = application_history.summarize(profile.home_dir, scope=scope,
            events_file=profile.analytics_events_file, seen_file=profile.seen_file)
        text = f"🕓 Поиски · {title}\n" + application_history.totals_text(applications)
        if not runs and applications["count"]:
            text += "\nСведения об откликах есть, но подробные записи поисков не сохранились."
        elif not runs and applications["profile_total"]:
            text += "\nВ этом направлении записей поисков пока нет. Сохранённые отклики доступны в других разделах ниже."
        else:
            text += "\n\n" + build_runs_text(runs, role=principal.get("role", "user"))
        if applications["recent"]:
            text += "\n\nПоследние сохранённые отклики:"
            for row in applications["recent"]:
                text += "\n• " + html.escape(str(row.get("title") or "Вакансия")[:120])
                if row.get("company"):
                    text += " · " + html.escape(str(row["company"])[:80])
        texts = [text]
    elif command in {"/chat_log", "/hunter_chat_log"}:
        _, legacy = bot._log_tail(profile_name, kind="chat_log")
        texts = [chat_text(profile.home_dir, view, legacy)]
    if scope == ALL and command in {"/stats", "/digest", "/research"}:
        summary = analytics.summarize(events_file=profile.analytics_events_file, scope=ALL, all_time=command == "/stats")
        texts[0] += "\n\nРешения по направлениям:\n" + "\n".join(
            f"• {html.escape(name)}: {count}" for name, count in summary["by_direction"].items())
    return title, texts, markup(profile.home_dir, command, scope=scope)


async def show(bot, chat_id, principal, profile_name, command, arg):
    if command in {"/log", "/hunter_log"}:
        await show_run(bot, chat_id, principal, profile_name, command, arg)
        return
    title, texts, controls = await read_thread(_history_report, bot, principal, profile_name, command, arg)
    # Telegram accepts one markup type per message. Keep the persistent section
    # keyboard, then put the scope controls on the actual report.
    await bot._send_text(chat_id, f"История откликов · {title}", reply_markup=bot._menu_reply_markup(principal))
    for text in texts:
        await bot._send_text(chat_id, text, reply_markup=controls)


def _saved_response_data(profile, *, days=30):
    import application_history
    sends = application_history.summarize(profile.home_dir, events_file=profile.analytics_events_file,
        seen_file=profile.seen_file, days=days, recent_limit=None)
    view = HistoryScope(profile.home_dir)
    key = ("response_outcomes", cache.stamp(profile.analytics_events_file),
           cache.ownership_key(view), profile_access.current_scope())
    try:
        latest = cache.memo(key, lambda: (_response_outcomes(profile, view), None))
    except (OSError, ValueError):
        latest = {}
        sends["incomplete"] = True
    return sends, view, latest


def responses_snapshot(profile, *, days=30):
    """Local saved-send totals and observed outcomes; never refresh hh.ru here."""
    from direction_history import vacancy_key
    from outcome import status_bucket, status_detail_bucket
    sends, view, latest = _saved_response_data(profile, days=days)
    viewed = invitations = rejected = 0
    for row in sends["recent"]:
        event = latest.get((view.owner(row), vacancy_key(row)), {})
        bucket = event.get("status_bucket") or status_bucket(event.get("status", ""))
        detail = event.get("status_detail_bucket") or status_detail_bucket(event.get("status", ""))
        viewed += bucket in {"positive", "rejected"} or detail == "pending_viewed"
        invitations += bucket == "positive"
        rejected += bucket == "rejected"
    return {"sent": sends["count"], "viewed": viewed,
            "invitations": invitations, "rejected": rejected,
            "waiting": max(0, sends["count"] - invitations - rejected), "incomplete": sends["incomplete"]}


def _applications_report(profile, command, page):
    from direction_history import vacancy_key
    from outcome import status_bucket
    sends, view, latest = _saved_response_data(profile)
    selected, title = APPLICATION_FILTERS[command]
    rows = []
    for row in sends["recent"]:
        event = latest.get((view.owner(row), vacancy_key(row)), {})
        bucket = event.get("status_bucket") or status_bucket(event.get("status", ""))
        if selected == "all" or bucket == selected:
            rows.append((row, bucket))
    pages = max(1, (len(rows) + APPLICATION_PAGE_SIZE - 1) // APPLICATION_PAGE_SIZE)
    page = min(page, pages - 1)
    lines = [f"{title} · за 30 дней · {view.title}", f"Сохранено: {len(rows)}"]
    if sends["incomplete"]:
        lines.append("Часть сохранённой истории недоступна. Повторите позже.")
    if not rows:
        lines.append("Здесь пока нет откликов.")
    labels = {"positive": "Приглашение", "rejected": "Отказ"}
    for row, bucket in rows[page * APPLICATION_PAGE_SIZE:(page + 1) * APPLICATION_PAGE_SIZE]:
        lines.extend(["", str(row.get("title") or "Вакансия")[:180],
                      str(row.get("company") or "Компания не указана")[:100],
                      str(row.get("created_at") or "")[:10] + " · " +
                      labels.get(bucket, "Работодатель ещё не ответил")])
    binding = token(profile.home_dir)
    controls = []
    paging = []
    for label, target in (("◀", page - 1), ("▶", page + 1)):
        if 0 <= target < pages:
            paging.append({"text": label, "callback_data": f"apps:{binding}:{selected}:{target}"})
    if paging:
        lines.append(f"\nСтраница {page + 1} из {pages}")
        controls.append(paging)
    controls.extend([[{"text": name, "callback_data": f"nav:{binding}:{route[1:]}"}]
                     for route, (_, name) in APPLICATION_FILTERS.items()])
    controls.append([{"text": "◀ К сводке", "callback_data": f"nav:{binding}:menu_responses"}])
    return "\n".join(lines), {"inline_keyboard": controls}


async def show_applications(bot, chat_id, principal, profile_name, command, arg):
    page = int(arg) if str(arg).isdigit() else 0
    text, controls = await read_thread(_applications_report, bot._profile(profile_name), command, page)
    await bot._send_text(chat_id, text, reply_markup=controls)


def _response_outcomes(profile, view):
    from direction_history import vacancy_key
    latest = {}
    for event in analytics.history_events(profile.analytics_events_file, compact=True):
        if event.get("event") == "negotiation_status":
            key = view.owner(event), vacancy_key(event)
            prior = latest.get(key, {})
            if str(event.get("created_at") or "") >= str(prior.get("created_at") or ""):
                latest[key] = event
    return latest
