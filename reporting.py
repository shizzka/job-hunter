"""Форматирование и вывод статистики Job Hunter."""
import json
import logging
import os

import analytics
import config
import seen

log = logging.getLogger("reporting")

SOURCE_ORDER = ("hh", "habr", "geekjob", "superjob")
SOURCE_LABELS = {
    "hh": "hh.ru",
    "habr": "Хабр",
    "geekjob": "GeekJob",
    "superjob": "SuperJob",
    "unknown": "unknown",
}
SOURCE_SHORT_LABELS = {
    "hh": "hh",
    "habr": "Хабр",
    "geekjob": "GJ",
    "superjob": "SJ",
    "unknown": "?",
}


def source_label(source: str, short: bool = False) -> str:
    labels = SOURCE_SHORT_LABELS if short else SOURCE_LABELS
    return labels.get(source, source)


def format_compact_source_counts(counts: dict[str, int]) -> str:
    parts = []
    for src in SOURCE_ORDER:
        if src not in counts:
            continue
        parts.append(f"{source_label(src, short=True)} {counts[src]}")
    return " ".join(parts) or "0"


def format_source_progress(prefix: str, src: str, current: int, total: int) -> str:
    label = source_label(src, short=True)
    if total > 0:
        return f"{prefix} {label} {current}/{total}"
    return f"{prefix} {label}"


def _format_stats_source_breakdown(by_source: dict) -> list[str]:
    lines = []
    for src in SOURCE_ORDER:
        bucket = by_source.get(src)
        if not bucket:
            continue
        lines.append(
            "  "
            f"{source_label(src):<10} total {bucket.get('total', 0):>4} | "
            f"applied {bucket.get('applied', 0):>3} | "
            f"manual {bucket.get('manual', 0):>3} | "
            f"skipped {bucket.get('skipped', 0):>3}"
        )

    for src, bucket in by_source.items():
        if src in SOURCE_ORDER:
            continue
        lines.append(
            "  "
            f"{src:<10} total {bucket.get('total', 0):>4} | "
            f"applied {bucket.get('applied', 0):>3} | "
            f"manual {bucket.get('manual', 0):>3} | "
            f"skipped {bucket.get('skipped', 0):>3}"
        )

    return lines


def _format_run_source_stats(source_stats: dict) -> str:
    if not source_stats:
        return "-"

    parts = []
    for src in SOURCE_ORDER:
        bucket = source_stats.get(src)
        if not bucket:
            continue
        parts.append(
            f"{source_label(src, short=True)} "
            f"new {bucket.get('new', 0)}"
            f"/rel {bucket.get('relevant', 0)}"
            f"/app {bucket.get('applied', 0)}"
            f"/man {bucket.get('manual', 0)}"
        )
    return "; ".join(parts) or "-"


def _format_analytics_source_breakdown(by_source: dict) -> list[str]:
    lines = []
    for src in SOURCE_ORDER:
        bucket = by_source.get(src)
        if not bucket:
            continue
        lines.append(
            "  "
            f"{source_label(src):<10} dec {bucket.get('decisions', 0):>4} | "
            f"auto {bucket.get('auto_applied', 0):>3} | "
            f"manual {bucket.get('manual', 0):>3} | "
            f"pos {bucket.get('positive', 0):>3} | "
            f"rej {bucket.get('rejected', 0):>3}"
        )

    for src, bucket in by_source.items():
        if src in SOURCE_ORDER:
            continue
        lines.append(
            "  "
            f"{src:<10} dec {bucket.get('decisions', 0):>4} | "
            f"auto {bucket.get('auto_applied', 0):>3} | "
            f"manual {bucket.get('manual', 0):>3} | "
            f"pos {bucket.get('positive', 0):>3} | "
            f"rej {bucket.get('rejected', 0):>3}"
        )
    return lines


def _format_top_query_breakdown(by_query: dict, limit: int = 5) -> list[str]:
    items = sorted(
        by_query.items(),
        key=lambda item: (
            -item[1].get("positive", 0),
            -item[1].get("auto_applied", 0),
            -item[1].get("decisions", 0),
            item[0],
        ),
    )[:limit]
    return [
        "  "
        f"{query[:42]:<42} | dec {bucket.get('decisions', 0):>3} | "
        f"auto {bucket.get('auto_applied', 0):>3} | "
        f"pos {bucket.get('positive', 0):>3} | "
        f"rej {bucket.get('rejected', 0):>3}"
        for query, bucket in items
    ]


def _format_resume_variant_breakdown(by_resume_variant: dict) -> list[str]:
    items = sorted(
        by_resume_variant.items(),
        key=lambda item: (
            -item[1].get("positive_rate", 0),
            -item[1].get("positive", 0),
            -item[1].get("applications", 0),
            item[0],
        ),
    )
    lines = []
    for variant, bucket in items:
        apps = bucket.get("applications", 0)
        viewed = bucket.get("viewed", 0)
        positive = bucket.get("positive", 0)
        rejected = bucket.get("rejected", 0)
        resp_rate = bucket.get("response_rate", 0)
        pos_rate = bucket.get("positive_rate", 0)
        lines.append(
            "  "
            f"{variant:<12} app {apps:>3} | "
            f"viewed {viewed:>3} | "
            f"pos {positive:>3} | "
            f"rej {rejected:>3} | "
            f"resp {resp_rate:>5.1f}% | "
            f"conv {pos_rate:>5.1f}%"
        )
    return lines


def load_recent_run_history(limit: int = 5) -> list[dict]:
    if limit <= 0 or not os.path.exists(config.RUN_HISTORY_FILE):
        return []

    try:
        with open(config.RUN_HISTORY_FILE, encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
    except Exception as exc:
        log.warning("Failed to read run history: %s", exc)
        return []

    items = []
    for line in lines[-limit:]:
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(items))


def print_stats():
    """Показать статистику."""
    s = seen.stats()
    recent_runs = load_recent_run_history(limit=5)
    analytics_summary = analytics.summarize()
    print(f"\n📊 Статистика Job Hunter")
    print(f"{'='*40}")
    print(f"  Всего обработано: {s['total']}")
    print(f"  Откликов:        {s['applied']}")
    print(f"  Ручной разбор:   {s['manual']}")
    print(f"  Пропущено:       {s['skipped']}")
    print()

    by_source = s.get("by_source", {})
    if by_source:
        print("По площадкам:")
        for line in _format_stats_source_breakdown(by_source):
            print(line)
        print()

    by_action = s.get("by_action", {})
    if by_action:
        print("Топ действий:")
        for action, count in sorted(by_action.items(), key=lambda item: (-item[1], item[0]))[:8]:
            print(f"  {action:<28} {count:>4}")
        print()

    print("Последние прогоны:")
    if not recent_runs:
        print("  пока нет истории запусков")
        print()
        return

    for run in recent_runs:
        status = "ok" if run.get("ok") else "err"
        created_at = run.get("created_at", "—")
        mode = run.get("mode", "search")
        found = run.get("found", 0)
        applied = run.get("applied", 0)
        skipped = run.get("skipped", 0)
        print(
            f"  {created_at} | {mode:<7} | {status:<3} | "
            f"found {found:<3} | applied {applied:<3} | skipped {skipped:<3}"
        )
        print(f"    {_format_run_source_stats(run.get('source_stats', {}))}")
        if run.get("error"):
            print(f"    error: {run['error']}")

    print()
    print(f"Аналитика за {analytics_summary['days']} дн.:")
    print(
        "  "
        f"Прогонов: {analytics_summary.get('search_runs', 0)} | "
        f"событий: {analytics_summary['events']} | "
        f"решений: {analytics_summary['decisions']} | "
        f"автооткликов: {analytics_summary['auto_applied']} | "
        f"manual: {analytics_summary['manual']}"
    )
    if analytics_summary.get("dry_run_matched", 0) > 0:
        print(f"  dry-run совпадений: {analytics_summary['dry_run_matched']}")
    print(
        "  "
        f"keyword skip: {analytics_summary['keyword_filtered']} | "
        f"red flags: {analytics_summary['red_flagged']} | "
        f"low score: {analytics_summary['low_score']}"
    )
    print(
        "  "
        f"инвайтов: {analytics_summary['invitations']} | "
        f"positive: {analytics_summary['positive_statuses']} | "
        f"rejected: {analytics_summary['rejected_statuses']} | "
        f"pending: {analytics_summary['pending_statuses']}"
    )

    if analytics_summary["events"] == 0:
        print("  Аналитика начнёт заполняться со следующего search/check.")

    if analytics_summary.get("by_source"):
        print()
        print("Аналитика по площадкам:")
        for line in _format_analytics_source_breakdown(analytics_summary["by_source"]):
            print(line)

    funnel = analytics_summary.get("funnel", {})
    if funnel.get("applied", 0) > 0:
        print()
        print("Воронка откликов:")
        print(f"  откликов:    {funnel['applied']:>4}")
        print(f"  просмотрено: {funnel['viewed']:>4}  ({funnel['response_rate']:.1f}%)")
        print(f"  ожидание:    {funnel['pending']:>4}")
        print(f"  отказ:       {funnel['rejected']:>4}")
        print(f"  позитив:     {funnel['positive']:>4}  ({funnel['positive_rate']:.1f}% от откликов)")

    if analytics_summary.get("by_resume_variant"):
        print()
        print("A/B резюме:")
        for line in _format_resume_variant_breakdown(analytics_summary["by_resume_variant"]):
            print(line)

    if analytics_summary.get("by_query"):
        print()
        print("Топ запросов:")
        for line in _format_top_query_breakdown(analytics_summary["by_query"]):
            print(line)

    if analytics_summary.get("top_decisions"):
        print()
        print("Топ решений:")
        for action, count in analytics_summary["top_decisions"]:
            print(f"  {action:<28} {count:>4}")
    print()
