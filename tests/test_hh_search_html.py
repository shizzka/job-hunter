from pathlib import Path

import pytest

from hh.search_html import parse_hh_search_html, search_antibot_kind

BASE = "https://spb.hh.ru/search/vacancy?text=QA"
FIXTURE = Path(__file__).parent / "fixtures" / "hh_search_background.html"


def parse(html):
    return parse_hh_search_html(html, base_url=BASE)


def card(href="/vacancy/123", response="/applicant/vacancy_response?vacancyId=123"):
    return f'<div data-qa="serp-item"><h3><a href="{href}">Tester</a></h3><a data-qa="serp-item__company">Company</a><span data-qa="serp-item__compensation">100</span><a data-qa="vacancy-serp__vacancy_response" href="{response}">Apply</a></div>'


def test_observed_modern_ssr_shape_without_executing_background_js():
    assert parse(FIXTURE.read_text()) == [{
        "id": "123456", "title": "QA Engineer", "company": "Example employer",
        "salary": "не указана", "url": "https://spb.hh.ru/vacancy/123456?from=search",
        "snippet": "Experience with testing.\nTest software.",
        "response_url": "https://spb.hh.ru/applicant/vacancy_response?vacancyId=123456",
    }]


def test_classic_card_and_id_deduplication():
    result = parse(card() + card())
    assert len(result) == 1
    assert result[0]["title"] == "Tester"
    assert result[0]["company"] == "Company"
    assert result[0]["salary"] == "100"


@pytest.mark.parametrize("name", ["hh_search_empty_page.html", "hh_search_no_results.html"])
def test_observed_empty_search_returns_empty_list(name):
    assert parse(FIXTURE.with_name(name).read_text()) == []


def test_empty_search_list_allows_css_build_hash_changes_and_whitespace():
    assert parse('<section data-qa="vacancy-serp__results"><ol class="vacancies-list--newhash">\n </ol></section>') == []


@pytest.mark.parametrize("html", [
    '<html><body>Unknown result</body></html>',
    '<section data-qa="vacancy-serp__results"></section>',
    '<ol class="vacancies-list--newhash"></ol>',
    '<section data-qa="vacancy-serp__results"><ol class="vacancies-list--newhash"><li>Redesigned card</li></ol></section>',
    '<section data-qa="vacancy-serp__results"><ol class="vacancies-list--newhash">Loading failed</ol></section>',
    '<template><div data-qa="empty-vacancy-search-block"></div></template>',
])
def test_missing_or_nonempty_results_structure_is_not_an_empty_search(html):
    with pytest.raises(ValueError, match="unrecognized HH search results"):
        parse(html)


@pytest.mark.parametrize("unsafe", [
    '<div id="captcha">Check</div>',
    '<div role="dialog">Unknown</div>',
    '<div data-qa="serp-item">Broken card</div>',
])
def test_empty_search_marker_does_not_hide_challenges_dialogs_or_broken_cards(unsafe):
    with pytest.raises(ValueError):
        parse(FIXTURE.with_name("hh_search_empty_page.html").read_text() + unsafe)


@pytest.mark.parametrize("tag", ["script", "style", "template", "noscript", "iframe", "object"])
def test_inert_fake_cards_are_ignored(tag):
    result = parse(f'<{tag}>{card("/vacancy/999")}</{tag}>' + card())
    assert [v["id"] for v in result] == ["123"]


@pytest.mark.parametrize("href", ["https://evil.example/vacancy/123", "https://evilhh.ru/vacancy/123", "https://user@hh.ru/vacancy/123", "http://hh.ru/vacancy/123", "javascript:alert(1)", "/vacancy/abc", "/vacancy/123/submit", "https://hh.ru:444/vacancy/123", "https://hh.ru\\@evil.example/vacancy/123"])
def test_invalid_vacancy_urls_fail_closed(href):
    with pytest.raises(ValueError):
        parse(card(href))


@pytest.mark.parametrize("href", ["https://evil.example/applicant/vacancy_response", "https://user@hh.ru/applicant/vacancy_response", "/other", "javascript:alert(1)"])
def test_invalid_response_urls_are_not_returned(href):
    assert parse(card(response=href))[0]["response_url"] == ""


def test_foreign_base_and_unknown_doc_fail_closed():
    with pytest.raises(ValueError):
        parse_hh_search_html(card(), base_url="https://evil.example/search/vacancy")
    with pytest.raises(ValueError):
        parse('<html><body>Unknown result</body></html>')


@pytest.mark.parametrize("html", ['<div role="dialog">Unknown</div>', '<dialog open>Unknown</dialog>', '<div aria-modal="true">Unknown</div>', '<div data-qa="unknown-modal">Unknown</div>'])
def test_unknown_modal_fails_closed(html):
    with pytest.raises(ValueError):
        parse(card() + html)


@pytest.mark.parametrize("html,kind", [('<div id="captcha">Check</div>', "captcha"), ('<iframe src="https://captcha.hh.ru/check"></iframe>', "captcha"), ('<p>DDoS-Guard</p>', "ddos_guard"), ('<p>Checking your browser</p>', "browser_check")])
def test_challenge_classification(html, kind):
    assert search_antibot_kind(html) == kind
    with pytest.raises(ValueError):
        parse(card() + html)


def test_inert_challenge_and_modal_text_does_not_classify():
    html = '<template><div role="dialog" id="captcha">DDoS-Guard</div></template><script>Checking your browser</script>'
    assert search_antibot_kind(html) == ""
    assert parse(card() + html)[0]["id"] == "123"


def test_established_challenge_priority_and_vocabulary():
    assert search_antibot_kind("<p>Checking your browser before accessing HH</p>") == "ddos_guard"
    assert search_antibot_kind('<p>Checking your browser</p><div id="captcha"></div>') == "captcha"
    assert search_antibot_kind("<p>Текст с картинки</p>") == "captcha"


def test_descendant_whitespace_and_block_labels():
    html = card().replace("Tester", "<span>QA </span><span>Engineer</span>")
    html = html.replace("Company", "<span>Example </span><span>Company</span>")
    assert parse(html)[0]["title"] == "QA Engineer"
    assert parse(html)[0]["company"] == "Example Company"
    html = card().replace("Company", "<div>Example</div><div>Company</div>")
    assert parse(html)[0]["company"] == "Example Company"


def test_trusted_link_fallback_preserves_metadata_and_dedup():
    html = '<section><h2><a href="/vacancy/123">QA Engineer</a></h2><div>Acme</div><div>от 100 000 руб</div></section>'
    results = parse(html + html)
    assert len(results) == 1
    assert results[0]["company"] == "Acme"
    assert results[0]["salary"] == "от 100 000 руб"
    assert results[0]["response_url"] == ""


@pytest.mark.parametrize("query", ["vacancyId=999", "vacancyId=123&vacancyId=999", "employerId=123", "vacancyId=123&resumeId=private", "vacancyId=123&letter=message", "vacancyId=123&coverLetter=message"])
def test_response_url_requires_matching_vacancy_and_metadata_only(query):
    assert parse(card(response="/applicant/vacancy_response?" + query))[0]["response_url"] == ""


def test_response_metadata_query_is_preserved():
    response = "/applicant/vacancy_response?vacancyId=123&employerId=456&hhtmFrom=search"
    assert parse(card(response=response))[0]["response_url"] == "https://spb.hh.ru" + response


def test_classic_passive_company_and_salary_css_fallbacks():
    html = '<div class="vacancy-serp-item__meta-info-company"><a>Outside company</a><div data-qa="serp-item"><h2><a href="/vacancy/123">QA Engineer</a></h2><div class="vacancy-serp-item__meta-info-company"><span><a>Acme</a></span></div><div class="vacancy-serp-item__sidebar">от 100 000 руб</div></div></div>'
    result = parse(html)[0]
    assert result["company"] == "Acme"
    assert result["salary"] == "от 100 000 руб"


def test_challenge_text_window_matches_existing_client():
    html = '<p>' + 'x' * 3001 + '</p><p>Verify you are human</p>'
    assert search_antibot_kind(html) == ""
    assert search_antibot_kind(html + '<div id="captcha"></div>') == "captcha"
