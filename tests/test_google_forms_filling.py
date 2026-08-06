import google_form_filler as legacy_google_forms
from google_forms import filling


def test_merge_fill_results_preserves_page_order():
    merged = filling._merge_fill_results(
        [
            {"filled": [{"index": 0}], "skipped": [{"index": 1}]},
            {"filled": [{"index": 2}], "skipped": []},
        ]
    )

    assert merged == {
        "filled": [{"index": 0}, {"index": 2}],
        "skipped": [{"index": 1}],
    }


def test_question_signature_normalizes_case_and_whitespace():
    questions = [
        {"question": "  QA   Experience "},
        {"question": "ТЕЛЕГРАМ"},
    ]

    assert filling._question_signature(questions) == ["qa experience", "телеграм"]


def test_legacy_module_reexports_pure_filling_helpers():
    helper_names = (
        "_google_form_preview_status",
        "_is_google_form_next_button_text",
        "_is_google_form_submit_button_text",
        "_looks_like_google_form_login_required",
        "_looks_like_google_form_submit_success",
        "_merge_fill_results",
        "_question_signature",
        "_reindex_page_questions",
    )

    for name in helper_names:
        assert getattr(legacy_google_forms, name) is getattr(filling, name)
