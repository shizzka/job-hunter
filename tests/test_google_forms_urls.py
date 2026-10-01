import asyncio

import google_form_filler as legacy_google_forms
from google_forms import urls


def test_normalize_google_form_url_unwraps_redirect_parameters():
    wrapped = (
        "https://hh.ru/away?to="
        "https%3A%2F%2Fdocs.google.com%2Fforms%2Fd%2Fe%2Fformid%2Fviewform%3Fusp%3Dsf_link"
    )

    assert urls.normalize_google_form_url(wrapped) == (
        "https://docs.google.com/forms/d/e/formid/viewform?usp=sf_link"
    )


def test_extract_google_form_urls_deduplicates_text_and_link_targets():
    form_url = "https://forms.gle/abc123"

    assert urls.extract_google_form_urls(
        f"Заполните {form_url}).",
        [{"href": form_url, "text": "Форма"}],
    ) == [form_url]


def test_google_form_url_validation_rejects_substring_and_private_host_bypasses():
    rejected = (
        "https://evil.example/login?next=docs.google.com/forms/d/e/x/viewform",
        "https://evil.example/forms.gle/abc",
        "http://127.0.0.1/admin?ref=docs.google.com/forms",
        "https://docs.google.com.evil.example/forms/d/e/x/viewform",
        "https://docs.google.com@evil.example/forms/d/e/x/viewform",
        "http://docs.google.com/forms/d/e/x/viewform",
        "https://docs.google.com/document/d/example",
    )

    assert all(urls.normalize_google_form_url(value) == "" for value in rejected)


def test_google_form_url_validation_accepts_supported_https_hosts():
    accepted = (
        "https://docs.google.com/forms/d/e/x/viewform",
        "https://forms.gle/abc123",
        "https://forms.google.com/example",
    )

    assert all(urls.normalize_google_form_url(value) == value for value in accepted)


def test_short_form_redirect_rejects_non_google_target(monkeypatch):
    class Response:
        is_redirect = True
        headers = {"location": "http://127.0.0.1:8080/admin"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url):
            return Response()

    monkeypatch.setattr(legacy_google_forms.httpx, "AsyncClient", lambda **kwargs: Client())

    assert asyncio.run(
        legacy_google_forms._resolve_google_form_redirect_url("https://forms.gle/abc123")
    ) == ""


def test_legacy_module_reexports_url_helpers():
    assert legacy_google_forms.FORM_URL_RE is urls.FORM_URL_RE
    assert legacy_google_forms.normalize_google_form_url is urls.normalize_google_form_url
    assert legacy_google_forms.extract_google_form_urls is urls.extract_google_form_urls
    assert legacy_google_forms._is_google_form_url is urls._is_google_form_url
    assert legacy_google_forms._strip_url_tail is urls._strip_url_tail
