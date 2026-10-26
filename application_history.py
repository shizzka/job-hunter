"""One read-only count of saved applications for statistics, history and HH UI.

Status observations are not send receipts. A receipt, its terminal decision and
the seen mark for one vacancy/direction describe the same saved application.
This is a local all-time history, not HH's current active/archived topic count.
"""
import json
from datetime import datetime, time, timedelta
from pathlib import Path

import analytics_read_cache as cache
import profile_access
from direction_history import ALL, UNSPECIFIED, LEGACY_TITLE, HistoryScope, has_identity, vacancy_key


def summarize(home, *, scope="active", events_file=None, seen_file=None, days=None, recent_limit=10):
    import analytics
    now = analytics._now()
    view = HistoryScope(home, scope)
    events_path = Path(events_file) if events_file else view.home / "analytics_events.jsonl"
    seen_path = Path(seen_file) if seen_file else view.home / "seen_vacancies.json"
    inputs = (cache.ownership_key(view), cache.stamp(events_path), cache.stamp(seen_path),
              profile_access.current_scope())
    key = ("applications", inputs, days, recent_limit,
           now.date().isoformat() if days is not None else None)
    return cache.memo(key, lambda: _summarize(view, events_path, seen_path, days=days, now=now,
                                            recent_limit=recent_limit, inputs=inputs), now=now.timestamp())


RUN_IDENTITY = ("direction", "resume_id", "resumeId", "requested_resume_id", "hh_resume_hash", "hh_resume_numeric_id")


def _send_or_uncertain(event):
    return ((event.get("event") == "decision" and event.get("decision") in {"applied_auto", "apply_uncertain"})
            or (event.get("event") == "application_result" and event.get("outcome") == "sent"))


def _relevant(events):
    """Sends plus the runs an anonymous send may be joined to; the ownership pass over the whole
    journal took ~5 s on a 230 MB profile (24.10). A run's owner depends only on that run's records."""
    sends = [e for e in events if _send_or_uncertain(e)]
    runs = {e.get("run_id") for e in sends if e.get("run_id") and not any(e.get(k) for k in RUN_IDENTITY)}
    return [e for e in events if _send_or_uncertain(e) or (
        e.get("run_id") in runs and e.get("event") in {"decision", "search_started", "search_finished"})]


def _applications(view, events_path, seen_path):
    """Recover saved applications once per input version, independently of the window."""
    incomplete = False
    try:
        events, _ = cache.journal(events_path).read(events_path)
    except (OSError, ValueError):
        events, incomplete = [], True
    events = view.events(_relevant(events))
    uncertain = {(e.get("run_id"), vacancy_key(e), view.owner(e)) for e in events
                 if e.get("event") == "decision" and e.get("decision") == "apply_uncertain"}
    receipts = [event for event in events if not event.get("dry_run") and event.get("mode") != "dry-run"
                and ((event.get("event") == "decision" and event.get("decision") == "applied_auto") or (
                    event.get("event") == "application_result" and event.get("outcome") == "sent"))]
    aliases = {(view.owner(e), e.get("run_id"), e["application_id"]): vacancy_key(e)
               for e in receipts if e.get("application_id") and e.get("vacancy_id")}
    applications = {}
    vacancies = set()
    for event in receipts:
        owner = view.owner(event)
        run = event.get("run_id")
        identity = vacancy_key(event)
        if (run, identity, owner) in uncertain:
            continue
        if not identity[1]:
            application_id = event.get("application_id")
            if not application_id:
                continue
            identity = aliases.get((owner, run, application_id), (identity[0], "application:" + application_id))
        if (run, identity, owner) in uncertain:
            continue
        key = owner, run, identity
        prior = applications.get(key, {})
        applications[key] = {**event, **prior}
        vacancies.add((owner, identity))
    try:
        data = json.loads(seen_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        data, incomplete = {}, True
    uncertain_vacancies = {(owner, identity) for _, identity, owner in uncertain}
    covered_vacancies = vacancies | uncertain_vacancies
    vacancy_owners = {}
    for owner, identity in vacancies:
        vacancy_owners.setdefault(identity, set()).add(owner)
    if isinstance(data, dict):
        for identity, row in data.items():
            if not isinstance(row, dict) or row.get("action") not in {"applied", "manual_ai_applied"}:
                continue
            saved_id = str(identity).split(":", 2)[-1] if str(identity).startswith("direction:") else identity
            identity = str(row.get("vacancy_id") or saved_id)
            source = row.get("source") or (identity.split(":", 1)[0] if ":" in identity else "hh")
            row = {**row, "source": source, "vacancy_id": identity.removeprefix(source + ":"),
                   "created_at": row.get("date", "")}
            owner, identity = view.owner(row), vacancy_key(row)
            # An anonymous legacy seen mark for a uniquely owned saved send is
            # another trace of that application, not an additional legacy send.
            owners = vacancy_owners.get(identity, set())
            if owner is None and len(owners) == 1 and None not in owners and not has_identity(row):
                continue
            if (owner, identity) not in covered_vacancies:
                applications[(owner, None, identity)] = row
    return applications, incomplete


def _summarize(view, events_path, seen_path, *, days=None, now=None, recent_limit=10, inputs=None):
    # A window boundary only filters saved applications. It must not repeat the
    # ownership/deduplication pass over the entire analytics journal.
    if inputs is None:
        inputs = (cache.ownership_key(view), cache.stamp(events_path), cache.stamp(seen_path),
                  profile_access.current_scope())
    import analytics
    now = now or analytics._now()
    def load_rows():
        rows = _applications(view, events_path, seen_path)
        return rows, now.timestamp() + 1 if rows[1] else None
    applications, incomplete = cache.memo(("application_rows", inputs), load_rows, now=now.timestamp())
    expiry = now + timedelta(seconds=1) if incomplete else None
    if days is not None:
        window = timedelta(days=max(0, days))
        cutoff = now - window
        midnight = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=now.tzinfo)
        expiry = min(expiry, midnight) if expiry is not None else midnight
        period = {}
        for key, row in applications.items():
            date = analytics._parse_dt(row.get("created_at"))
            if date is None:
                continue
            if cutoff <= date <= now:
                period[key] = row
                # The existing rolling cutoff includes equality: the row leaves
                # one datetime tick after date + window, not at that instant.
                expiry = min(expiry, date + window + timedelta(microseconds=1))
            elif date > now:
                expiry = min(expiry, date)
        applications = period
    selected = [row for row in applications.values() if view.matches(row)]
    restricted = bool(profile_access.current_scope()[1])
    totals = selected if restricted else list(applications.values())
    legacy = [row for row in totals if view.owner(row) in {None, ""}]
    summary = {"count": len(selected), "profile_total": len(totals), "legacy_total": len(legacy),
            "scope": view.name, "direction_title": view.title,
            "recent": sorted(selected, key=lambda row: str(row.get("created_at") or ""), reverse=True)[:recent_limit],
            "restricted": restricted, "incomplete": incomplete}
    return summary, expiry.timestamp() if expiry is not None else None


def totals_text(summary):
    prefix = "не меньше " if summary["incomplete"] else ""
    lines = [f"Сохранено откликов за всё время: {prefix}{summary['count']}"]
    if not summary["restricted"] and summary["scope"] != ALL:
        lines.append(f"Всего в профиле: {prefix}{summary['profile_total']}")
    if summary["legacy_total"] and summary["scope"] != UNSPECIFIED:
        lines.append(f"{LEGACY_TITLE}: {summary['legacy_total']}")
    if summary["incomplete"]:
        lines.append("Часть сохранённой истории не удалось прочитать. Повторите позже или сообщите администратору.")
    return "\n".join(lines)
