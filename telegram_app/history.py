"""Direction-filtered read-only history screens and their bound navigation."""
import hashlib
import html
import re
from pathlib import Path

import analytics
import profile_access
from direction_history import ALL, UNSPECIFIED, HistoryScope
from state_store.private_journal import read_json_records

COMMANDS = {"/stats", "/digest", "/research", "/runs", "/log", "/hunter_log", "/chat_log", "/hunter_chat_log"}


def token(home):
    view = HistoryScope(home)
    return hashlib.sha256((str(Path(home).resolve()) + "|" + view.name).encode()).hexdigest()[:12]


def markup(home, command):
    return {"inline_keyboard": [[{"text": label, "callback_data": f"hist:{token(home)}:{command.removeprefix('/')}:{scope}"}]
            for label, scope in (("Это направление", "active"), ("Все направления", "all"), ("Направление не указано", "unspecified"))
            if not profile_access.current_scope()[1] or scope == "active"]}


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


async def show(bot, chat_id, principal, profile_name, command, arg):
    from telegram_bot_ui import build_stats_text, build_runs_text, build_run_summary_text
    profile = bot._profile(profile_name)
    scope = scope_arg(arg)
    view = HistoryScope(profile.home_dir, scope)
    title = html.escape(view.title)
    texts = []
    if command == "/stats":
        snapshot = bot._stats_snapshot(profile_name, scope=scope)
        snapshots = None
        if principal.get("role") == "admin":
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
        runs = bot._recent_runs(profile_name, limit=5, scope=scope)
        texts = [f"🕓 Прогоны · {title}\n" + build_runs_text(runs)]
    elif command in {"/log", "/hunter_log"}:
        summary = analytics.get_run_summary(None if scope != "active" or arg == "active" else arg or None,
            events_file=profile.analytics_events_file, history_file=profile.run_history_file, profile=profile_name, scope=scope)
        texts = [f"📊 Последний запуск · {title}\n" + build_run_summary_text(summary, profile_name=profile_name)]
        if summary and scope == ALL:
            texts[0] = f"Направление: {html.escape(summary['direction_title'])}\n" + texts[0]
    elif command in {"/chat_log", "/hunter_chat_log"}:
        _, legacy = bot._log_tail(profile_name, kind="chat_log")
        texts = [chat_text(profile.home_dir, view, legacy)]
    if scope == ALL and command in {"/stats", "/digest", "/research"}:
        summary = analytics.summarize(events_file=profile.analytics_events_file, scope=ALL, all_time=command == "/stats")
        texts[0] += "\n\nРешения по направлениям:\n" + "\n".join(
            f"• {html.escape(name)}: {count}" for name, count in summary["by_direction"].items())
    # Telegram accepts one markup type per message. Keep the persistent section
    # keyboard, then put the scope controls on the actual report.
    await bot._send_text(chat_id, f"История откликов · {title}", reply_markup=bot._menu_reply_markup(principal))
    for text in texts:
        await bot._send_text(chat_id, text, reply_markup=markup(profile.home_dir, command))
