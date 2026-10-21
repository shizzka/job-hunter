"""Direction work conditions decided from HH fields, never matcher opinions."""
from __future__ import annotations

import re

import config

FORMAT_LABELS = {"remote": "удалённо", "hybrid": "гибрид",
                 "on_site": "на месте работодателя", "field": "разъездная работа"}
_CODES = {"REMOTE": "remote", "HYBRID": "hybrid", "ON_SITE": "on_site", "FIELD_WORK": "field"}


def defaults(direction: dict) -> dict:
    profiles = direction.get("search_profiles") or []
    remote = bool(profiles) and all(p.get("schedule") == "remote" for p in profiles)
    return {"allowed_formats": ["remote", "hybrid"] if remote else list(FORMAT_LABELS),
            "allow_fly_in_fly_out": False, "allowed_cities": []}


def validate(value: dict) -> dict:
    if (not isinstance(value, dict) or set(value) - {"allowed_formats", "allow_fly_in_fly_out", "allowed_cities"}
            or not isinstance(value.get("allowed_formats"), list) or not value["allowed_formats"]
            or any(not isinstance(f, str) or f not in FORMAT_LABELS for f in value["allowed_formats"])
            or type(value.get("allow_fly_in_fly_out")) is not bool
            or not isinstance(value.get("allowed_cities", []), list)
            or len(value.get("allowed_cities", [])) > 30
            or any(not isinstance(c, str) or not c.strip() or len(c) > 100 for c in value.get("allowed_cities", []))):
        raise ValueError("Укажите допустимые форматы работы и разрешена ли вахта.")
    return {"allowed_formats": list(dict.fromkeys(value["allowed_formats"])),
            "allow_fly_in_fly_out": value["allow_fly_in_fly_out"],
            "allowed_cities": list(dict.fromkeys(c.strip() for c in value.get("allowed_cities", [])))}


def from_hh_fields(item: dict) -> dict:
    """HH web state and public API shapes. A full-day/shift schedule proves no format."""
    formats = []
    raw = item.get("workFormats", item.get("work_format", []))
    for group in raw if isinstance(raw, list) else []:
        elements = group.get("workFormatsElement", [group.get("id")]) if isinstance(group, dict) else [group]
        for code in elements if isinstance(elements, list) else []:
            if isinstance(code, str):
                formats.append(_CODES.get(code, "unknown"))
            else:
                formats.append("unknown")
    schedule = item.get("@workSchedule", item.get("schedule"))
    if isinstance(schedule, dict):
        schedule = schedule.get("id")
    if not formats and schedule == "remote":
        formats = ["remote"]
    obj = lambda v: v if isinstance(v, dict) else {}
    compensation = obj(item.get("compensation"))
    salary_range = obj(item.get("salary_range"))
    period = obj(salary_range.get("period"))
    employment = obj(item.get("employment_form"))
    durations = item.get("flyInFlyOutDurations")
    durations = durations if isinstance(durations, list) else []
    fly = (schedule == "flyInFlyOut" or item.get("employmentForm") == "FLY_IN_FLY_OUT"
           or employment.get("id") == "FLY_IN_FLY_OUT"
           or compensation.get("mode") == "FLY_IN_FLY_OUT"
           or period.get("id") == "FLY_IN_FLY_OUT"
           or any(isinstance(g, dict) and g.get("flyInFlyOutDurationsElement") for g in durations))
    result = {"formats": sorted(set(formats)), "fly_in_fly_out": fly}
    city = obj(item.get("address")).get("city")
    if isinstance(city, str) and city.strip():
        result["city"] = city.strip()
    return result


def from_hh_page(html: str, vacancy_id: str) -> dict:
    """Read only vacancy-owned state and labelled DOM fields, excluding description."""
    from hh.observation import initial_state
    from hh.search_html import _tree
    formats, dom_formats, cities, fly, shift_mention = set(), set(), set(), False, False
    try:
        state = initial_state(html)
        view = state.get("vacancyView") or {}
        full = view.get("vacancyFull") or {}
        for item in (full.get("vacancy"), view.get("shortVacancy"), full.get("shortVacancy")):
            if not isinstance(item, dict) or str(item.get("vacancyId", "")) != str(vacancy_id):
                continue
            fields = from_hh_fields(item)
            if formats and fields["formats"] and formats != set(fields["formats"]):
                formats.add("unknown")
            formats.update(fields["formats"])
            fly |= fields["fly_in_fly_out"]
            if fields.get("city"):
                cities.add(fields["city"])
            shift_mention |= bool(re.search("вахт", str(item.get("name", "")) + str(item.get("description", "")), re.I))
    except (ValueError, TypeError, AttributeError):
        pass
    tree = _tree(html)
    for node in tree.root.walk():
        qa = node.attrs.get("data-qa")
        if qa not in {"work-formats-text", "common-employment-text", "vacancy-salary", "vacancy-title", "vacancy-description"}:
            continue
        text = node.text().casefold().replace("ё", "е")
        # Labels inside free vacancy copy cannot manufacture structured evidence.
        parent = node.parent
        in_description = False
        while parent is not None:
            if (parent.attrs.get("data-qa") == "vacancy-description"
                    or "vacancy-description" in parent.attrs.get("class", "").split()):
                in_description = True
                break
            parent = parent.parent
        if qa in {"vacancy-title", "vacancy-description"}:
            shift_mention |= "вахт" in text
        if in_description:
            continue
        if qa == "work-formats-text":
            value = text.removeprefix("формат работы:").strip()
            labels = {"удаленно": "remote", "гибрид": "hybrid",
                      "на месте работодателя": "on_site", "разъездной": "field",
                      "разъездная работа": "field"}
            parts = re.split(r"\s*(?:,| или | и )\s*", value)
            dom_formats.update(labels.get(part, "unknown") for part in parts)
        elif qa == "common-employment-text" and re.fullmatch(r"вахта(?: на \d+ смен[уы]?)?", text):
            fly = True
        elif qa == "vacancy-salary" and re.search(r"\bза вахту\b", text):
            fly = True
    if formats and dom_formats and formats != dom_formats:
        formats.add("unknown")
    formats.update(dom_formats)
    result = {"formats": sorted(formats), "fly_in_fly_out": fly}
    if len(cities) == 1:
        result["city"] = cities.pop()
    if shift_mention:
        result["shift_mention"] = True
    return result


def check(vacancy: dict, details: str = "", *, direction=None) -> dict:
    """allow / reject / review. Legacy profiles and other sources keep their policy."""
    direction = getattr(config, "ACTIVE_DIRECTION", None) if direction is None else direction
    if vacancy.get("source", "hh") != "hh" or not direction:
        return {"status": "allow", "reason": ""}
    policy = validate(direction.get("work_conditions", defaults(direction)))
    fields = vacancy.get("_hh_work_conditions") or from_hh_fields(vacancy)
    if fields.get("fly_in_fly_out") and not policy["allow_fly_in_fly_out"]:
        return {"status": "reject", "reason": "Вахта не разрешена в этом направлении."}
    formats = set(fields.get("formats") or [])
    if formats and "unknown" not in formats and not formats.intersection(policy["allowed_formats"]):
        labels = ", ".join(FORMAT_LABELS.get(f, f) for f in sorted(formats))
        return {"status": "reject", "reason": f"Формат работы не подходит: {labels}."}
    if not formats or "unknown" in formats:
        return {"status": "review", "reason": "формат не указан"}
    if policy["allowed_cities"] and "remote" not in formats.intersection(policy["allowed_formats"]):
        city = fields.get("city")
        if not city:
            return {"status": "review", "reason": "Город места работы не указан; нужно проверить, требуется ли переезд."}
        normalize_city = lambda c: " ".join(c.casefold().replace("ё", "е").split())
        if normalize_city(city) not in {normalize_city(c) for c in policy["allowed_cities"]}:
            return {"status": "reject", "reason": f"Место работы: {city}. В этом направлении работа в другом городе не разрешена."}
    free_text = "\n".join(str(vacancy.get(k) or "") for k in ("title", "snippet", "details")) + "\n" + details
    if (fields.get("shift_mention") or re.search(r"вахт", free_text, re.I)) and not fields.get("fly_in_fly_out"):
        return {"status": "review", "reason": "В тексте упомянута вахта; нужно проверить условия работы."}
    return {"status": "allow", "reason": ""}


def unevaluated(result: dict) -> dict:
    return {"score": None, "should_apply": False, "red_flags": [],
            "evaluation_status": "not_evaluated", "work_conditions_status": result["status"],
            "reason": result["reason"]}


def apply_block(result: dict) -> dict:
    return {"ok": False, "reason": "work_conditions", "work_conditions_status": result["status"],
            "work_conditions_reason": result["reason"], "message": result["reason"]}


async def recheck_before_submit(session, vacancy_url: str, vacancy_context: str = "") -> dict:
    """Fresh passive GET of the actual vacancy while retaining the owned response form."""
    if not getattr(config, "ACTIVE_DIRECTION", None):
        return {"status": "allow", "reason": ""}
    from hh.recovery import AUTH_URL, _auth_cookies
    from hh.ui import ensure_session_ui, HHUnexpectedUI
    from hh.vacancy_details import safe_navigation_url
    from hh.search_html import search_antibot_kind
    url = safe_navigation_url(vacancy_url)
    if not url:
        return {"status": "review", "reason": "Не удалось проверить условия работы."}
    page, context = session._page, session._context
    request, attempt = context.request, getattr(session, "_external_attempt", None)
    guard = session._ui_guard

    async def fresh():
        if (session._page is not page or session._context is not context
                or context.request is not request or session._ui_guard is not guard
                or getattr(session, "_external_attempt", None) is not attempt):
            session._hh_recovery_stop_reason = "browser_ownership_changed"
            session._hh_recovery_uncertain = True
        await ensure_session_ui(session, "work_conditions", allowed=("response",))
        if attempt is not None and (attempt.acting or attempt.uncertain or attempt.command_started):
            raise ValueError("Application may already have started")

    response = None
    try:
        await fresh()
        before = _auth_cookies(await context.cookies([AUTH_URL]))
        await fresh()
        response = await request.get(url, max_redirects=0, timeout=15000, headers={"Cache-Control": "no-cache"})
        await fresh()
        if (response.url != url or response.status != 200
                or response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/html"):
            raise ValueError("Vacancy unavailable")
        html = await response.text()
        await fresh()
        if search_antibot_kind(html) or re.search(r'"userType"\s*:\s*"anonymous"', html):
            raise ValueError("Vacancy access unproven")
        after = _auth_cookies(await context.cookies([AUTH_URL]))
        await fresh()
        if before != after:
            session._hh_recovery_stop_reason = "auth_cookie_changed"
            await fresh()
            raise ValueError("Authentication changed")
        fields = from_hh_page(html, url.rstrip("/").split("/")[-1])
        result = check({"_hh_work_conditions": fields, "title": vacancy_context})
        await response.dispose()
        response = None
        await fresh()
        return result
    except HHUnexpectedUI:
        raise
    except Exception:
        return {"status": "review", "reason": "Не удалось проверить условия работы; отклик не отправлен."}
    finally:
        if response is not None:
            await response.dispose()
