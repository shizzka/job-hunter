"""Synthetic HH conditions: no services or candidate data."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
import directions
import work_conditions as wc
from hh.search_html import parse_hh_search_html
from tests.test_directions import saved


def policy(**overrides):
    return {"work_conditions": {**wc.defaults({}), **overrides}}


def vacancy(**fields):
    return {"id": "42", "title": "Электромонтажник", "source": "hh",
            "_hh_work_conditions": wc.from_hh_fields(fields)}


# «only remote» excludes hybrid unless the direction opts in (owner 24.10)
@pytest.mark.parametrize("code,expected", [("REMOTE", "allow"), ("HYBRID", "reject"),
                                           ("ON_SITE", "reject"), ("FIELD_WORK", "reject")])
def test_remote_direction_uses_vacancy_format_not_search_schedule(code, expected):
    direction = {"search_profiles": [{"area": 113, "schedule": "remote"}]}
    v = vacancy(work_format=[{"id": code}], schedule={"id": "fullDay"})
    assert wc.check(v, direction=direction)["status"] == expected


@pytest.mark.parametrize("schedule", ["", "fullDay", "shift", "flexible"])
def test_unknown_format_never_authorizes_apply_when_formats_are_restricted(schedule):
    result = wc.check(vacancy(schedule={"id": schedule}), direction=policy(allowed_formats=["remote", "hybrid"]))
    assert result == {"status": "review", "reason": "формат не указан"}


@pytest.mark.parametrize("schedule", ["", "fullDay", "shift", "flexible"])
def test_unknown_format_is_irrelevant_when_every_format_is_allowed(schedule):
    assert wc.check(vacancy(schedule={"id": schedule}), direction=policy())["status"] == "allow"
    # the other gates still apply: shift work and a restricted city
    assert wc.check(vacancy(schedule={"id": "flyInFlyOut"}), direction=policy())["status"] == "reject"
    assert wc.check({**vacancy(schedule={"id": schedule}), "snippet": "Вахта 30/30"},
                    direction=policy())["status"] == "review"
    assert wc.check(vacancy(schedule={"id": schedule}),
                    direction=policy(allowed_cities=["Москва"]))["status"] == "review"


def test_search_state_enriches_ssr_and_additional_cards_without_using_query():
    items = [{"vacancyId": i, "name": "Электромонтажник",
              "workFormats": [{"workFormatsElement": ["ON_SITE"]}],
              "@workSchedule": "flyInFlyOut"} for i in (41, 42)]
    html = ('<div data-qa="serp-item"><h3><a href="/vacancy/41">Электромонтажник</a></h3></div>'
            '<template id="HH-Lux-InitialState">' + json.dumps({"vacancySearchResult": {"vacancies": items}}) + '</template>')
    rows = parse_hh_search_html(html, base_url="https://hh.ru/search/vacancy?schedule=remote")
    assert len(rows) == 2
    assert all(wc.check(v, direction=policy())["status"] == "reject" for v in rows)


@pytest.mark.parametrize("label", [
    '<div data-qa="common-employment-text"><span>Вахта на 30 смен</span></div>',
    '<div data-qa="vacancy-salary">от 100 000 ₽ за вахту, на руки</div>',
])
def test_observed_structured_dom_shift_work(label):
    html = '<p data-qa="work-formats-text">Формат работы: на месте работодателя</p>' + label
    fields = wc.from_hh_page(html, "42")
    assert fields == {"formats": ["on_site"], "fly_in_fly_out": True}
    v = {"_hh_work_conditions": fields}
    assert wc.check(v, direction=policy())["status"] == "reject"
    assert wc.check(v, direction=policy(allow_fly_in_fly_out=True))["status"] == "allow"


def test_full_vacancy_state_is_scoped_to_requested_vacancy():
    item = {"vacancyId": 42, "workFormats": [{"workFormatsElement": ["REMOTE"]}]}
    html = '<template id="HH-Lux-InitialState">' + json.dumps({
        "vacancyView": {"vacancyFull": {"vacancy": item}},
        "recommendations": [{"vacancyId": 43, "@workSchedule": "flyInFlyOut"}],
    }) + '</template>'
    assert wc.from_hh_page(html, "42") == {"formats": ["remote"], "fly_in_fly_out": False}
    assert wc.from_hh_page(html, "43")["formats"] == []


def test_conflicting_page_fields_cannot_broaden_allowed_formats():
    item = {"vacancyId": 42, "workFormats": ["ON_SITE"]}
    html = ('<template id="HH-Lux-InitialState">' + json.dumps({
        "vacancyView": {"vacancyFull": {"vacancy": item}}}) + '</template>'
        '<p data-qa="work-formats-text">Формат работы: удалённо</p>')
    v = {"_hh_work_conditions": wc.from_hh_page(html, "42")}
    assert wc.check(v, direction=policy(allowed_formats=["remote"]))["status"] == "review"


def test_free_text_only_requests_review_even_if_shift_work_allowed():
    html = ('<p data-qa="work-formats-text">Формат работы: удалённо</p>'
            '<div data-qa="vacancy-description">Вахтовый посёлок'
            '<p data-qa="common-employment-text">Вахта на 30 смен</p></div>')
    v = {"title": "Электромонтажник на вахту", "_hh_work_conditions": wc.from_hh_page(html, "42")}
    assert v["_hh_work_conditions"]["fly_in_fly_out"] is False
    assert wc.check(v, direction=policy(allow_fly_in_fly_out=True))["status"] == "review"


def test_city_restriction_uses_workplace_city_not_publication_region():
    direction = policy(allowed_cities=["Город Пример"])
    fields = {"workFormats": ["ON_SITE"], "area": {"name": "Город Пример"},
              "address": {"city": "Другой город"}}
    assert wc.check(vacancy(**fields), direction=direction)["status"] == "reject"
    del fields["address"]
    assert wc.check(vacancy(**fields), direction=direction)["status"] == "review"
    fields["address"] = {"city": "Город Пример"}
    assert wc.check(vacancy(**fields), direction=direction)["status"] == "allow"
    assert wc.check(vacancy(workFormats=["REMOTE"]), direction=direction)["status"] == "allow"


def test_remote_only_direction_takes_hybrid_only_as_an_explicit_choice():
    direction = {"search_profiles": [{"area": 113, "schedule": "remote"}]}
    assert wc.check(vacancy(workFormats=["HYBRID"]), direction=direction)["status"] == "reject"
    direction["work_conditions"] = {**wc.defaults(direction), "allowed_formats": ["remote", "hybrid"]}
    assert wc.check(vacancy(workFormats=["HYBRID"]), direction=direction)["status"] == "allow"


def test_allowed_vacancy_and_other_source_policy(monkeypatch):
    direction = policy()
    for code in ("REMOTE", "HYBRID", "ON_SITE", "FIELD_WORK"):
        assert wc.check(vacancy(workFormats=[code]), direction=direction)["status"] == "allow"
    assert wc.check({"source": "habr"}, direction=direction)["status"] == "allow"
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", None, raising=False)
    assert wc.check({"source": "hh"})["status"] == "allow"


def test_new_and_existing_direction_defaults_are_read_only_until_saved(tmp_path):
    value = saved(tmp_path)
    assert value["work_conditions"] == wc.defaults({})
    path = tmp_path / "directions" / "qa.json"
    raw = json.loads(path.read_text())
    del raw["work_conditions"]
    raw["search_profiles"] = [{"area": 113, "schedule": "remote"}]
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    loaded = directions.load_direction(tmp_path, "qa")
    assert path.read_bytes() == before
    assert loaded["work_conditions"] == {"allowed_formats": ["remote"], "allow_fly_in_fly_out": False, "allowed_cities": []}
    loaded["search_profiles"].append({"area": 2, "schedule": ""})
    del loaded["work_conditions"]
    assert directions.validate_direction(loaded)["work_conditions"] == wc.defaults({})
    loaded["work_conditions"] = {"allowed_formats": ["field"], "allow_fly_in_fly_out": True, "allowed_cities": []}
    assert directions.save_direction(tmp_path, loaded)["work_conditions"] == loaded["work_conditions"]


@pytest.mark.parametrize("conditions", [{}, {"allowed_formats": []},
    {"allowed_formats": ["remote"], "allow_fly_in_fly_out": "нет"},
    {"allowed_formats": ["other"], "allow_fly_in_fly_out": False}])
def test_invalid_conditions_fail_closed(tmp_path, conditions):
    value = saved(tmp_path)
    value["work_conditions"] = conditions
    with pytest.raises(directions.DirectionError):
        directions.validate_direction(value)


@pytest.mark.parametrize("variant,expected", [("allowed", "allow"), ("fly", "reject"),
    ("unknown", "review"), ("redirect", "review"), ("cookies", "review")])
def test_fresh_pre_submit_reads_actual_vacancy_without_navigation(monkeypatch, variant, expected):
    # remote-only: an unknown format must go to review (with every format allowed it would not matter)
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", policy(allowed_formats=["remote", "hybrid"]), raising=False)
    html = '<p data-qa="work-formats-text">Формат работы: удалённо</p>'
    if variant == "fly":
        html += '<div data-qa="common-employment-text">Вахта на 30 смен</div>'
    elif variant == "unknown":
        html = '<main>Описание работы</main>'
    response = SimpleNamespace(url="https://hh.ru/vacancy/42", status=302 if variant == "redirect" else 200,
        headers={"content-type": "text/html"}, text=AsyncMock(return_value=html), dispose=AsyncMock())
    request = SimpleNamespace(get=AsyncMock(return_value=response))
    cookies = [{"name": "hhtoken", "value": "synthetic"}]
    context = SimpleNamespace(request=request, cookies=AsyncMock(side_effect=[
        cookies, [{"name": "hhtoken", "value": "changed"}] if variant == "cookies" else cookies]))
    page = SimpleNamespace(goto=AsyncMock())
    session = SimpleNamespace(_context=context, _page=page, _ui_guard=object(), _ensure_expected_ui=AsyncMock())
    result = asyncio.run(wc.recheck_before_submit(session, response.url))
    assert result["status"] == expected
    request.get.assert_awaited_once_with(response.url, max_redirects=0, timeout=15000, headers={"Cache-Control": "no-cache"})
    response.dispose.assert_awaited_once()
    page.goto.assert_not_awaited()
