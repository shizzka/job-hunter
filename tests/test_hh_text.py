from hh.text import compact_text, normalize_text


def test_normalize_text_collapses_whitespace_and_case():
    assert normalize_text("  Поднять\nРЕЗЮМЕ  ") == "поднять резюме"


def test_compact_text_removes_whitespace_and_normalizes_case():
    assert compact_text("  Archived \n TRUE  ") == "archivedtrue"
