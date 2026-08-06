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


def test_legacy_module_reexports_url_helpers():
    assert legacy_google_forms.FORM_URL_RE is urls.FORM_URL_RE
    assert legacy_google_forms.normalize_google_form_url is urls.normalize_google_form_url
    assert legacy_google_forms.extract_google_form_urls is urls.extract_google_form_urls
    assert legacy_google_forms._is_google_form_url is urls._is_google_form_url
    assert legacy_google_forms._strip_url_tail is urls._strip_url_tail
