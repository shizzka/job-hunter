"""Read-only hh market snapshot for arbitrary vacancy titles (admin research tool).

Only GET requests to the public search page are made, paced and bounded. No
responses, no browser, no profile state changes. Results are aggregate numbers
for posts; nothing here influences applications.
"""
from __future__ import annotations

import csv
import io
import re
import statistics
import time
from collections import Counter
from datetime import datetime

from hh.observation import initial_state

MAX_QUERIES = 20
MAX_PAGES = 20          # 20 × 50 = up to 1000 vacancies per title
PAGE_SIZE = 50
PAUSE_SECONDS = 2.0
NET_FACTOR = 0.87       # gross → net estimate with the base 13% personal income tax

EXPERIENCE = {
    "noExperience": "без опыта",
    "between1And3": "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6": "6+ лет",
}
FORMATS = {"REMOTE": "удалёнка", "HYBRID": "гибрид", "ON_SITE": "офис", "FIELD_WORK": "разъездная"}


class MarketResearchError(Exception):
    pass


def parse_queries(text: str) -> list[str]:
    queries = list(dict.fromkeys(" ".join(line.split()) for line in str(text or "").splitlines() if line.strip()))
    if not queries:
        raise MarketResearchError("Отправьте хотя бы одно название вакансии.")
    if len(queries) > MAX_QUERIES:
        raise MarketResearchError(f"Не больше {MAX_QUERIES} названий за раз.")
    if any(len(q) > 100 for q in queries):
        raise MarketResearchError("Название вакансии — не длиннее 100 символов.")
    return queries


def _stem_tokens(text: str) -> set[str]:
    return {word[:5] for word in re.findall(r"[a-zа-яё0-9]+", str(text).casefold().replace("ё", "е")) if len(word) > 1}


def strict_match(query: str, title: str) -> bool:
    """Every word of the query (by a 5-letter stem) appears in the vacancy title."""
    return _stem_tokens(query) <= _stem_tokens(title)


def parse_compensation(value) -> dict:
    """Monthly rouble bounds, net-estimated; other currencies/modes are kept as unknown."""
    result = {"salary_from": None, "salary_to": None, "salary_currency": "", "salary_gross": None,
              "salary_from_net": None, "salary_to_net": None}
    if not isinstance(value, dict) or "noCompensation" in value:
        return result
    currency = str(value.get("currencyCode") or "")
    result["salary_currency"] = currency
    result["salary_gross"] = bool(value.get("gross")) if "gross" in value else None
    if str(value.get("mode") or "MONTH") != "MONTH":
        return result
    for side in ("from", "to"):
        amount = value.get(side)
        if isinstance(amount, (int, float)) and amount > 0:
            result[f"salary_{side}"] = int(amount)
            if currency == "RUR":
                result[f"salary_{side}_net"] = int(round(amount * (NET_FACTOR if value.get("gross") else 1.0)))
    return result


def _row(query: str, vacancy: dict) -> dict:
    formats = []
    for group in vacancy.get("workFormats") or []:
        if isinstance(group, dict):
            formats.extend(group.get("workFormatsElement") or [])
    company = vacancy.get("company") or {}
    area = vacancy.get("area") or {}
    published = (vacancy.get("publicationTime") or {}).get("$", "")
    row = {
        "query": query,
        "vacancy_id": vacancy.get("vacancyId"),
        "title": str(vacancy.get("name") or ""),
        "strict_match": strict_match(query, vacancy.get("name") or ""),
        "company": str(company.get("visibleName") or company.get("name") or ""),
        "area": str(area.get("name") or ""),
        "experience": EXPERIENCE.get(str(vacancy.get("workExperience") or ""), str(vacancy.get("workExperience") or "")),
        "formats": ", ".join(FORMATS.get(f, f) for f in dict.fromkeys(formats)),
        "published": published[:10],
        "responses": vacancy.get("totalResponsesCount"),
        "url": f"https://hh.ru/vacancy/{vacancy.get('vacancyId')}",
    }
    row.update(parse_compensation(vacancy.get("compensation")))
    return row


def collect(client, queries: list[str], *, area: int = 113, max_pages: int = MAX_PAGES,
            pause: float = PAUSE_SECONDS, sleep=time.sleep, progress=None) -> dict:
    """Return {"rows": [...], "totals": {query: hh_total}} using GET search pages only."""
    rows, totals = [], {}
    for query in queries:
        seen = set()
        for page in range(max_pages):
            response = client.get("https://hh.ru/search/vacancy", params={
                "text": query, "search_field": "name", "area": area,
                "items_on_page": PAGE_SIZE, "page": page, "order_by": "publication_time",
            })
            if response.status_code != 200:
                raise MarketResearchError(f"hh ответил {response.status_code} на поиск «{query}».")
            try:
                state = initial_state(response.text)
            except ValueError as exc:
                raise MarketResearchError("hh вернул страницу без данных поиска (возможно, проверка браузера).") from exc
            result = state.get("vacancySearchResult") or {}
            if page == 0:
                totals[query] = int(result.get("totalResults") or 0)
            vacancies = [v for v in result.get("vacancies") or [] if isinstance(v, dict)]
            fresh = [v for v in vacancies if v.get("vacancyId") not in seen]
            for vacancy in fresh:
                seen.add(vacancy.get("vacancyId"))
                rows.append(_row(query, vacancy))
            if progress:
                progress(query, page, len(seen), totals.get(query, 0))
            if not fresh or len(vacancies) < PAGE_SIZE or len(seen) >= totals.get(query, 0):
                break
            sleep(pause)
        sleep(pause)
    return {"rows": rows, "totals": totals, "collected_at": datetime.now().isoformat(timespec="seconds")}


def _quantiles(values: list[int]) -> dict:
    if not values:
        return {}
    values = sorted(values)
    if len(values) == 1:
        return {"p25": values[0], "median": values[0], "p75": values[0]}
    q = statistics.quantiles(values, n=4, method="inclusive")
    return {"p25": int(q[0]), "median": int(statistics.median(values)), "p75": int(q[2])}


def summarize(snapshot: dict, *, strict: bool = True) -> list[dict]:
    result = []
    for query, total in snapshot.get("totals", {}).items():
        rows = [r for r in snapshot.get("rows", []) if r["query"] == query and (r["strict_match"] or not strict)]
        salaried = [r for r in rows if r["salary_from_net"] or r["salary_to_net"]]
        lows = [r["salary_from_net"] for r in salaried if r["salary_from_net"]]
        highs = [r["salary_to_net"] for r in salaried if r["salary_to_net"]]
        mids = [((r["salary_from_net"] or r["salary_to_net"]) + (r["salary_to_net"] or r["salary_from_net"])) // 2
                for r in salaried]
        responses = [r["responses"] for r in rows if isinstance(r["responses"], int)]
        by_exp = {}
        for label in list(EXPERIENCE.values()):
            group = [r for r in salaried if r["experience"] == label]
            if group:
                by_exp[label] = {"count": len(group), **_quantiles(
                    [((r["salary_from_net"] or r["salary_to_net"]) + (r["salary_to_net"] or r["salary_from_net"])) // 2
                     for r in group])}
        result.append({
            "query": query,
            "hh_total": total,
            "vacancies": len(rows),
            "with_salary": len(salaried),
            "salary_share": round(len(salaried) / len(rows) * 100, 1) if rows else 0.0,
            "from": _quantiles(lows), "to": _quantiles(highs), "mid": _quantiles(mids),
            "by_experience": by_exp,
            "experience_mix": dict(Counter(r["experience"] for r in rows).most_common()),
            "remote_share": round(sum("удалёнка" in r["formats"] for r in rows) / len(rows) * 100, 1) if rows else 0.0,
            "top_areas": Counter(r["area"] for r in rows).most_common(5),
            "top_companies": Counter(r["company"] for r in rows).most_common(5),
            "responses_median": int(statistics.median(responses)) if responses else None,
        })
    return result


def _money(value) -> str:
    return f"{value / 1000:.0f} тыс." if value else "—"


def summary_text(summary: list[dict], *, strict: bool = True) -> str:
    lines = ["📈 Рынок вакансий hh.ru (вся Россия, поиск по названию"
             + (", строгое совпадение слов)" if strict else ")"),
             "Зарплаты — «на руки» (gross × 0,87), в месяц, только рубли.", ""]
    for item in summary:
        mid = item["mid"]
        lines.append(f"🔹 {item['query']}: вакансий {item['vacancies']} (hh всего {item['hh_total']})")
        lines.append(f"   с зарплатой: {item['with_salary']} ({item['salary_share']:.0f}%)".replace(".", ","))
        if mid:
            lines.append(f"   середина вилки: медиана {_money(mid['median'])}, 25–75%: "
                         f"{_money(mid['p25'])} – {_money(mid['p75'])}")
            if item["from"]:
                lines.append(f"   «от»: медиана {_money(item['from']['median'])}; "
                             f"«до»: медиана {_money(item['to'].get('median'))}")
        for label, stats in item["by_experience"].items():
            lines.append(f"   опыт {label}: {stats['count']} вак., медиана {_money(stats['median'])}")
        lines.append(f"   удалёнка: {item['remote_share']:.0f}%".replace(".", ",")
                     + (f"; откликов на вакансию (медиана): {item['responses_median']}"
                        if item["responses_median"] is not None else ""))
        if item["top_areas"]:
            lines.append("   города: " + ", ".join(f"{a} {n}" for a, n in item["top_areas"][:3]))
        lines.append("")
    lines.append("Отчёт .md и все данные .csv — файлами ниже.")
    return "\n".join(lines)


def export_csv(snapshot: dict) -> bytes:
    columns = ["query", "strict_match", "title", "company", "area", "experience", "formats",
               "salary_from", "salary_to", "salary_currency", "salary_gross", "salary_from_net",
               "salary_to_net", "responses", "published", "url"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, delimiter=";", extrasaction="ignore")
    writer.writeheader()
    for row in snapshot.get("rows", []):
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8-sig")


def export_markdown(snapshot: dict, summary: list[dict]) -> bytes:
    """Post-ready report: summary per title plus the salaried vacancies table."""
    lines = [f"# Рынок вакансий hh.ru — {snapshot.get('collected_at', '')[:16].replace('T', ' ')}", "",
             "Источник: поиск hh.ru по названию вакансии, вся Россия. Зарплаты — в месяц, «на руки»",
             "(для «до вычета налогов» × 0,87), только рубли. Строгое совпадение: все слова запроса есть в названии.", ""]
    for item in summary:
        mid = item["mid"]
        lines += [f"## {item['query']}", "",
                  f"- вакансий (строгое совпадение): **{item['vacancies']}** из {item['hh_total']} в выдаче hh",
                  f"- с указанной зарплатой: **{item['with_salary']}** ({item['salary_share']:.0f}%)".replace(".", ",")]
        if mid:
            lines.append(f"- середина вилки: медиана **{_money(mid['median'])}**, 25–75%: {_money(mid['p25'])} – {_money(mid['p75'])}")
        if item["from"]:
            lines.append(f"- «от»: медиана {_money(item['from']['median'])}; «до»: медиана {_money(item['to'].get('median'))}")
        for label, stats in item["by_experience"].items():
            lines.append(f"- опыт {label}: {stats['count']} вак., медиана {_money(stats['median'])}")
        lines.append(f"- удалёнка: {item['remote_share']:.0f}%".replace(".", ","))
        if item["responses_median"] is not None:
            lines.append(f"- откликов на вакансию (медиана): {item['responses_median']}")
        if item["top_areas"]:
            lines.append("- города: " + ", ".join(f"{a} — {n}" for a, n in item["top_areas"]))
        if item["top_companies"]:
            lines.append("- чаще всего нанимают: " + ", ".join(f"{c} ({n})" for c, n in item["top_companies"]))
        rows = [r for r in snapshot.get("rows", []) if r["query"] == item["query"] and r["strict_match"]
                and (r["salary_from_net"] or r["salary_to_net"])]
        if rows:
            lines += ["", "| Вакансия | Компания | Город | Опыт | От | До | Откликов |", "|---|---|---|---|---|---|---|"]
            for r in sorted(rows, key=lambda r: -(r["salary_to_net"] or r["salary_from_net"] or 0)):
                cell = lambda v: str(v).replace("|", "/")
                lines.append(f"| [{cell(r['title'])}]({r['url']}) | {cell(r['company'])} | {cell(r['area'])} | "
                             f"{r['experience']} | {_money(r['salary_from_net'])} | {_money(r['salary_to_net'])} | "
                             f"{r['responses'] if r['responses'] is not None else '—'} |")
        lines.append("")
    return "\n".join(lines).encode("utf-8")
