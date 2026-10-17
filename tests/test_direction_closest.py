import hashlib
from pathlib import Path

import pytest

import directions


def _variant(tmp_path, title, text):
    root = tmp_path / "directions" / "el"
    root.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    path = (root / f"{digest}.md").resolve()
    path.write_bytes(data)
    return {"hh_resume_hash": hashlib.md5(title.encode()).hexdigest(), "hh_resume_numeric_id": None, "title": title,
            "text_file": str(path), "text_sha256": digest, "fetched_at": "2026-10-17T10:00:00+00:00"}


@pytest.fixture
def direction(tmp_path):
    return {"name": "el", "title": "Электрика", "filter_policy": "generic", "search_queries": ["электрик"],
            "relevant_keywords": [], "exclude_keywords": [], "selection_strategy": "closest",
            "variants": [_variant(tmp_path, "Электромонтажник", "Монтаж кабельных трасс, сборка щитов, СКС, видеонаблюдение."),
                         _variant(tmp_path, "Техник-электрик", "Обслуживание и ремонт электрооборудования, диагностика, техник.")]}


@pytest.mark.parametrize("text,expected", [
    ("Техник-электрик\nОбслуживание электрооборудования здания", "Техник-электрик"),
    ("Электромонтажник\nМонтаж кабельных трасс и щитов", "Электромонтажник"),
    ("Монтажник СКС\nПрокладка кабельных трасс, видеонаблюдение", "Электромонтажник"),
    ("Сервисный инженер\nремонт и диагностика электрооборудования", "Техник-электрик"),
])
def test_closest_prefers_matching_resume(direction, text, expected):
    assert directions.select_variant(direction, "1", text)["title"] == expected


def test_closest_is_deterministic_and_falls_back_to_first(direction):
    first = directions.select_variant(direction, "1", "")
    assert first["title"] == "Электромонтажник"
    assert first["selection_reason"].startswith("closest:lexical")
    assert directions.select_variant(direction, "1", "Техник") == directions.select_variant(direction, "2", "Техник")


def test_closest_skips_paused_variants(direction):
    direction["variants"][1]["status"] = "paused"
    assert directions.select_variant(direction, "1", "Техник-электрик")["title"] == "Электромонтажник"
