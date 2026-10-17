import json
import os
from dataclasses import FrozenInstanceError

import pytest

from kb_retrieval import (
    KnowledgeBudgetError, KnowledgeIndex, KnowledgeSourceError,
    get_knowledge_index, retrieval_enabled, retrieve_knowledge,
)


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_bm25_returns_relevant_complete_chunks(tmp_path):
    write(tmp_path, "knowledge/projects.md", "# Проекты\n## Android\nПроверял мобильные push-уведомления.\n\n## Excel\nВёл таблицы Excel.")
    result = retrieve_knowledge("андроид", profile_dir=tmp_path)
    assert "Проекты > Android" in result
    assert "push-уведомления" in result
    assert result.index("push-уведомления") < result.index("Вёл таблицы")


def test_lowercase_yo_and_light_russian_stemming(tmp_path):
    write(tmp_path, "knowledge/kb.md", "# Кейсы\n## Другое\nПроверял Android.\n## Отчеты\nГотовил отчёты для клиента.")
    result = retrieve_knowledge("отчетами", profile_dir=tmp_path)
    assert result.index("Готовил отчёты") < result.index("Проверял Android")


@pytest.mark.parametrize("query", ["автоматизации", "автоматизацию", "автоматизацией"])
def test_inflected_automation_alias_ranks_first(tmp_path, query):
    write(tmp_path, "knowledge/kb.md", "# Опыт\n- [CONFIRMED] Ручная проверка.\n- [CONFIRMED] automation scripting.")
    result = retrieve_knowledge(query, profile_dir=tmp_path)
    assert result.index("automation") < result.index("Ручная проверка")


@pytest.mark.parametrize("query", ["REST API", "CI/CD", "3D", "1с", "эксель", "автоматизация"])
def test_preserves_tools_and_builtin_aliases(tmp_path, query):
    write(tmp_path, "knowledge/tools.md", "# Другое\nНерелевантный фрагмент.\n# Инструменты\nИспользовал REST API, CI/CD, 3D, 1C, Excel, automation.")
    result = retrieve_knowledge(query, profile_dir=tmp_path)
    assert result.index("Использовал") < result.index("Нерелевантный")
    assert {"rest", "api", "ci/cd", "3d"} <= set(get_knowledge_index(tmp_path).chunks[1].tokens)


def test_optional_synonyms_are_profile_local_and_invalidate_cache(tmp_path):
    write(tmp_path, "knowledge/tools.md", "# Другое\nНерелевантный фрагмент.\n# Инструменты\nПрименял Playwright.")
    before = get_knowledge_index(tmp_path)
    write(tmp_path, "knowledge/synonyms.txt", "# comment\nplaywright = драматург\n")
    result = retrieve_knowledge("драматург", profile_dir=tmp_path)
    assert result.index("Playwright") < result.index("Нерелевантный")
    after = get_knowledge_index(tmp_path)
    assert before is not after
    assert dict(after.aliases).get("драматург") == "playwright"


def test_tagged_items_split_even_on_one_line(tmp_path):
    write(tmp_path, "knowledge/kb.md", "# Опыт\n- [CONFIRMED] Android проверки - [LIMIT] Не заявлять AQA - [CONFIRMED] Excel таблицы")
    result = retrieve_knowledge("Android", profile_dir=tmp_path)
    assert "Android проверки" in result
    assert "[LIMIT] Не заявлять AQA" in result
    assert result.index("Android проверки") < result.index("Excel таблицы")
    assert len(get_knowledge_index(tmp_path).chunks) == 3


def test_inline_numbered_topics_provide_heading_context(tmp_path):
    write(tmp_path, "knowledge/kb.md", "# Факты\n1. Excel - [CONFIRMED] Создавал отчёты. - [LIMIT] Не аналитик. 2. Android - [CONFIRMED] Проверял уведомления.")
    index = get_knowledge_index(tmp_path)
    assert len(index.chunks) == 3
    assert index.chunks[0].heading == "Факты > Excel"
    assert index.chunks[2].heading == "Факты > Android"
    result = index.retrieve("Android")
    assert result.index("Проверял уведомления") < result.index("Создавал отчёты")
    assert "2. Android" not in index.chunks[1].text


@pytest.mark.parametrize("prefix", ["", "- ", "* ", "+ "])
@pytest.mark.parametrize("tag", ["LIMIT", "DO_NOT_CLAIM"])
def test_boundary_tags_without_dash_bullet_fail_closed(tmp_path, prefix, tag):
    write(tmp_path, "knowledge/kb.md", f"# Факты\n{prefix}[{tag}] Не заявлять AQA.")
    index = get_knowledge_index(tmp_path)
    assert index.chunks[0].mandatory
    with pytest.raises(KnowledgeBudgetError):
        index.retrieve("", limit_chars=0)


def test_all_limits_and_core_profile_always_included(tmp_path):
    write(tmp_path, "knowledge/profile_note.md", "# Профиль\nРучной QA начинающего уровня.")
    write(tmp_path, "knowledge/kb.md", "# Опыт\n- [DO_NOT_CLAIM] Не заявлять коммерческую автоматизацию.\n- [LIMIT] Нет SQL production.\n- [CONFIRMED] Android smoke.")
    result = retrieve_knowledge("погода", profile_dir=tmp_path)
    assert "Ручной QA" in result
    assert "Не заявлять коммерческую автоматизацию" in result
    assert "Нет SQL production" in result
    assert result.index("Нет SQL production") < result.index("Android smoke")


def test_mandatory_heading_propagates_through_hierarchy(tmp_path):
    write(tmp_path, "knowledge/kb.md", "# LIMIT\n## Автоматизация\nНе заявлять самостоятельную разработку.\n# Практика\nExcel отчёты.")
    result = retrieve_knowledge("", profile_dir=tmp_path)
    assert "Не заявлять самостоятельную разработку" in result
    assert result.index("Не заявлять самостоятельную разработку") < result.index("Excel отчёты")


@pytest.mark.parametrize("heading", ["Карьерная цель", "Границы проекта", "Ограничения", "Критические запреты"])
def test_boundary_words_do_not_make_untagged_section_mandatory(tmp_path, heading):
    write(tmp_path, "knowledge/kb.md", f"# {heading}\nОбсуждал ограничения, границы и запреты.\n- [FACT] Использовал Excel.")
    index = get_knowledge_index(tmp_path)
    assert not any(chunk.mandatory for chunk in index.chunks)
    assert index.retrieve("погода", limit_chars=0) == ""


@pytest.mark.parametrize("heading", ["DO_NOT_CLAIM", "FORBIDDEN_CLAIMS", "DO NOT CLAIM", "[LIMIT]"])
def test_explicit_boundary_sections_remain_mandatory(tmp_path, heading):
    write(tmp_path, "knowledge/kb.md", f"# {heading}\n## Уровень\nНе заявлять Senior.\n# Практика\nПроверял Android.")
    index = get_knowledge_index(tmp_path)
    required = [c for c in index.chunks if c.mandatory]
    assert len(required) == 1
    assert required[0].text == "Не заявлять Senior."
    assert index.retrieve("погода", limit_chars=len(required[0].render())) == required[0].render()


def test_flattened_horizontal_rule_ends_tagged_boundary(tmp_path):
    write(tmp_path, "knowledge/candidate_facts.md",
          "# Факты\n16. Карьерная цель - [LIMIT] Не заявлять текущий доход. "
          "--- 17. Позиционирование Manual QA: Excel. "
          "--- 18. Запреты - [DO_NOT_CLAIM] Не заявлять Senior. "
          "--- 19. Правила использования: выбирать релевантные факты.")
    index = get_knowledge_index(tmp_path)
    required = "\n\n".join(c.render() for c in index.chunks if c.mandatory)
    result = index.retrieve("погода", limit_chars=len(required))
    assert "[LIMIT] Не заявлять текущий доход." in result
    assert "[DO_NOT_CLAIM] Не заявлять Senior." in result
    assert "Manual QA" not in result and "Правила использования" not in result
    assert "Manual QA" in index.retrieve("Позиционирование", limit_chars=1000)


@pytest.mark.parametrize("key", ["weak", "allowed_wording", "do_not_claim", "forbidden_claims"])
def test_normalized_mandatory_duplicates_keep_markdown_and_every_distinct_ban(tmp_path, key):
    write(tmp_path, "facts.json", json.dumps({key: [
        "НЕ заявлять ＡＱＡ.", "Не заявлять коммерческий SQL.", "Не заявлять AQA без проверки.",
    ]}, ensure_ascii=False))
    write(tmp_path, "knowledge/candidate_facts.md", "# Факты\n- [LIMIT] Не заявлять AQA.\n- [DO_NOT_CLAIM] Не заявлять\nкоммерческий SQL.")
    write(tmp_path, "candidate_interview.json", json.dumps({"facts": [{
        "text": "[LIMIT] НЕ заявлять AQA.", "topic": "общий",
    }]}))
    index = get_knowledge_index(tmp_path)
    required = [c for c in index.chunks if c.mandatory]
    assert len(required) == 2  # One Markdown section and one distinct JSON ban.
    result = index.retrieve("погода", limit_chars=len("\n\n".join(c.render() for c in required)))
    assert result.count("[LIMIT] Не заявлять AQA.") == 1
    assert "[DO_NOT_CLAIM] Не заявлять\nкоммерческий SQL." in result
    assert "Не заявлять AQA без проверки." in result  # No substring deduplication.
    assert "ＡＱＡ" not in result and "candidate_interview.json" not in result
    with pytest.raises(KnowledgeBudgetError):
        index.retrieve("погода", limit_chars=len(result) - 1)


def test_all_tagged_boundaries_survive_core_cap_and_unrelated_query(tmp_path):
    write(tmp_path, "knowledge/profile_note.md", "Краткий профиль. " * 200
          + "\n\n- [LIMIT] Нет коммерческого SQL.\n- [DO_NOT_CLAIM] Не заявлять Senior.")
    write(tmp_path, "knowledge/candidate_facts.md", "# Опыт\n- [LIMIT] Не заявлять AQA.\n- [DO_NOT_CLAIM] Не заявлять DevOps.")
    write(tmp_path, "candidate_interview.json", json.dumps({"facts": [{
        "text": "Проверял Android.\n- [LIMIT] Нет опыта Go.", "topic": "общий",
    }]}))
    index = get_knowledge_index(tmp_path)
    required = "\n\n".join(c.render() for c in index.chunks if c.mandatory)
    result = index.retrieve("погода", limit_chars=len(required))
    for boundary in ("Нет коммерческого SQL.", "Не заявлять Senior.", "Не заявлять AQA.",
                     "Не заявлять DevOps.", "Нет опыта Go."):
        assert boundary in result
    with pytest.raises(KnowledgeBudgetError):
        index.retrieve("погода", limit_chars=len(required) - 1)


def test_long_core_prefix_is_bounded_and_tail_remains_retrievable(tmp_path):
    prefix = " ".join(f"Профиль{i}." for i in range(400))
    tail = "Уникальный опыт Blender: моделировал детали."
    write(tmp_path, "knowledge/profile_note.md", prefix + "\n\n" + tail)
    index = get_knowledge_index(tmp_path)
    required = "\n\n".join(c.render() for c in index.chunks if c.mandatory)
    assert 1500 <= len(required) < 1600  # Prefix plus source label/formatting.
    assert tail not in required
    mandatory_words = " ".join(c.text for c in index.chunks if c.mandatory).split()
    assert mandatory_words == prefix.split()[:len(mandatory_words)]
    assert any(tail in c.text and not c.mandatory for c in index.chunks)
    assert tail in index.retrieve("Blender", limit_chars=len(required) + 1200)
    assert index.retrieve("погода", limit_chars=len(required)) == required


def test_constraints_file_and_core_alias_are_mandatory(tmp_path):
    write(tmp_path, "knowledge/constraints.md", "Не готов к ночным сменам.")
    write(tmp_path, "knowledge/core_profile.md", "Механик.")
    result = retrieve_knowledge("", profile_dir=tmp_path)
    assert "Механик" in result
    assert "Не готов к ночным сменам" in result


def test_long_sections_split_with_heading_context_and_bound(tmp_path):
    text = "\n\n".join(f"Android абзац {i}. " + "проверка " * 75 for i in range(8))
    write(tmp_path, "knowledge/kb.md", "# Проекты\n## Мобильные\n" + text)
    index = get_knowledge_index(tmp_path)
    assert len(index.chunks) >= 5
    assert all(len(chunk.text) <= 1100 for chunk in index.chunks)
    assert all(chunk.heading == "Проекты > Мобильные" for chunk in index.chunks)
    assert all(f"Android абзац {i}" in "\n".join(chunk.text for chunk in index.chunks) for i in range(8))
    result = index.retrieve("Android", limit_chars=1500)
    assert len(result) <= 1500
    assert "Проекты > Мобильные" in result


def test_single_long_paragraph_and_unbroken_string_are_bounded(tmp_path):
    write(tmp_path, "knowledge/kb.md", "# Android\n" + "word " * 1800 + "x" * 4000)
    index = get_knowledge_index(tmp_path)
    assert all(len(chunk.text) <= 1100 for chunk in index.chunks)
    assert len(index.retrieve("Android", limit_chars=1300)) <= 1300


def test_overflow_fails_closed_even_for_unrelated_query(tmp_path):
    prohibition = "Нельзя заявлять коммерческий опыт. " * 100
    write(tmp_path, "knowledge/kb.md", "# LIMIT\n" + prohibition)
    index = get_knowledge_index(tmp_path)
    with pytest.raises(KnowledgeBudgetError, match="boundaries cannot be omitted"):
        index.retrieve("Excel", limit_chars=100)
    result = index.retrieve("Excel", limit_chars=10000)
    assert prohibition.strip() in result


def test_exact_budget_is_deterministic_and_no_partial_chunks(tmp_path):
    write(tmp_path, "knowledge/kb.md", "# Опыт\n- [LIMIT] Не AQA.\n- [CONFIRMED] Android smoke.\n- [CONFIRMED] Android регресс.")
    index = get_knowledge_index(tmp_path)
    minimum = "\n\n".join(chunk.render() for chunk in index.chunks if chunk.mandatory)
    assert index.retrieve("Android", limit_chars=len(minimum)) == minimum
    result = index.retrieve("Android", limit_chars=105)
    assert len(result) <= 105
    assert result == index.retrieve("Android", limit_chars=105)
    assert all("### " + chunk.source in result for chunk in index.chunks if chunk.text in result)
    with pytest.raises(KnowledgeBudgetError):
        index.retrieve("", limit_chars=len(minimum) - 1)


@pytest.mark.parametrize("budget", [-1, 1.5, True, None])
def test_invalid_budget(tmp_path, budget):
    with pytest.raises(ValueError, match="limit_chars"):
        retrieve_knowledge("anything", profile_dir=tmp_path, limit_chars=budget)


def test_confirmed_json_only_with_constraints_preserved(tmp_path):
    write(tmp_path, "facts.json", json.dumps({
        "confirmed": {"tools": "Android"}, "unconfirmed": {"tools": "Kubernetes"},
        "inferred": {"level": "Senior"}, "legacy": "AWS",
        "weak": ["SQL учебный"], "allowed_wording": ["Знаком с SQL"],
        "do_not_claim": ["Нет опыта DevOps"], "forbidden_claims": ["Не руководитель"],
    }, ensure_ascii=False))
    result = retrieve_knowledge("Android Kubernetes Senior AWS", profile_dir=tmp_path)
    assert "Android" in result
    assert "Kubernetes" not in result and "Senior" not in result and "AWS" not in result
    assert "SQL учебный" in result and "Знаком с SQL" in result
    assert "Нет опыта DevOps" in result and "Не руководитель" in result


def test_legacy_flat_json_is_not_evidence(tmp_path):
    write(tmp_path, "facts.json", '{"tools": "Android"}')
    assert not retrieve_knowledge("Android", profile_dir=tmp_path)


def test_interview_confirmed_facts_and_unconditional_boundaries(tmp_path):
    write(tmp_path, "candidate_interview.json", json.dumps({"facts": [
        {"text": "Проверял Android", "topic": "мобильное", "source": "telegram_confirmed"},
        {"text": "Не AQA", "topic": "границы", "source": "telegram_confirmed"},
        {"text": "Нет коммерческого SQL", "topic": "ограничения"},
        {"text": "Использовал Excel", "topic": "таблицы"},
        {"text": 123},
    ]}, ensure_ascii=False))
    result = retrieve_knowledge("андроид", profile_dir=tmp_path)
    assert "Проверял Android" in result and "Не AQA" in result
    assert "Нет коммерческого SQL" in result
    assert result.index("Проверял Android") < result.index("Использовал Excel")


def test_long_interview_fact_is_chunked_and_tail_is_retrievable(tmp_path):
    write(tmp_path, "candidate_interview.json", json.dumps({"facts": [{
        "text": ("Выполнял ручные проверки интерфейса. " * 400) + "\n\nРазрабатывал Blender 3D модели.",
        "topic": "проекты", "source": "telegram_confirmed",
    }]}, ensure_ascii=False))
    index = get_knowledge_index(tmp_path)
    assert all(len(chunk.text) <= 1100 for chunk in index.chunks)
    result = index.retrieve("Blender", limit_chars=1500)
    assert "Blender 3D модели" in result
    assert len(result) <= 1500


def test_profile_cache_and_source_updates_no_leak(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    path = write(first, "knowledge/kb.md", "# Android\nFIRST candidate.")
    write(second, "knowledge/kb.md", "# Android\nSECOND candidate.")
    snapshot = get_knowledge_index(first)
    assert get_knowledge_index(first) is snapshot
    assert "SECOND" not in retrieve_knowledge("Android", profile_dir=first)
    assert "FIRST" not in retrieve_knowledge("Android", profile_dir=second)
    old_mtime = path.stat().st_mtime_ns
    path.write_text("# Android\nTHIRD candidate.", encoding="utf-8")
    os.utime(path, ns=(old_mtime + 1000, old_mtime + 1000))
    updated = get_knowledge_index(first)
    assert updated is not snapshot
    assert "THIRD" in updated.retrieve("Android")
    assert "FIRST" in snapshot.retrieve("Android")
    path.unlink()
    assert not retrieve_knowledge("Android", profile_dir=first)


def test_eval_questions_and_unrelated_txt_are_not_indexed(tmp_path):
    write(tmp_path, "knowledge/kb_eval_questions.txt", "Android ANSWER LEAK")
    write(tmp_path, "knowledge/misc.txt", "Android WRONG")
    assert not retrieve_knowledge("Android", profile_dir=tmp_path)


@pytest.mark.parametrize("kind", ["file", "directory", "facts", "synonyms"])
def test_symlink_escape_is_rejected(tmp_path, kind):
    root = tmp_path / "profile"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = write(outside, "secret.md", "OTHER PROFILE SECRET")
    if kind == "directory":
        (root / "knowledge").symlink_to(outside, target_is_directory=True)
    elif kind == "facts":
        (root / "facts.json").symlink_to(target)
    else:
        (root / "knowledge").mkdir()
        (root / "knowledge" / ("leak.md" if kind == "file" else "synonyms.txt")).symlink_to(target)
    with pytest.raises(KnowledgeSourceError):
        get_knowledge_index(root)


def test_malformed_json_fails_closed(tmp_path):
    write(tmp_path, "facts.json", "{not JSON}")
    with pytest.raises(KnowledgeSourceError):
        get_knowledge_index(tmp_path)


def test_snapshot_roundtrip_is_immutable_and_does_not_read_files(tmp_path):
    path = write(tmp_path, "knowledge/kb.md", "# Android\n- [LIMIT] Не AQA\n- [CONFIRMED] Smoke.")
    index = get_knowledge_index(tmp_path)
    serialized = index.to_snapshot()
    assert json.loads(json.dumps({"kb": serialized}))["kb"] == serialized
    path.unlink()
    restored = KnowledgeIndex.from_snapshot(serialized)
    assert restored == index
    assert restored.retrieve("Android") == index.retrieve("Android")
    assert "Smoke" in restored.retrieve("Android")
    with pytest.raises(FrozenInstanceError):
        restored.chunks = ()


def test_flag_defaults_enabled_and_accepts_false_values(monkeypatch):
    monkeypatch.delenv("KB_RETRIEVAL_ENABLED", raising=False)
    assert retrieval_enabled()
    for value in ("0", "false", "False", " no ", "OFF"):
        monkeypatch.setenv("KB_RETRIEVAL_ENABLED", value)
        assert not retrieval_enabled()
    monkeypatch.setenv("KB_RETRIEVAL_ENABLED", "1")
    assert retrieval_enabled()


def test_missing_profile_is_empty_and_zero_budget_supported(tmp_path):
    assert retrieve_knowledge("Android", profile_dir=tmp_path / "missing", limit_chars=0) == ""


def test_named_directions_can_share_only_common_knowledge(tmp_path):
    common = write(tmp_path, "knowledge/candidate_facts.md",
                   "- [FACT] Монтаж кабеля.\n- [LIMIT] Без ночных смен.")
    profiles = []
    for name in ("qa", "electrician"):
        root = tmp_path / "profiles" / name
        root.mkdir(parents=True)
        (root / "knowledge").symlink_to(common.parent, target_is_directory=True)
        write(root, "facts.json", json.dumps({"confirmed": {"private": name}}))
        profiles.append(root)
    qa, electrician = (get_knowledge_index(root) for root in profiles)
    assert "Монтаж кабеля" in qa.retrieve("кабеля")
    assert "Монтаж кабеля" in electrician.retrieve("кабеля")
    assert "[LIMIT] Без ночных смен." in electrician.retrieve("кабеля")
    assert "private: electrician" in electrician.retrieve("")
    assert "private: qa" not in electrician.retrieve("")
    common.write_text("- [FACT] Монтаж щитов.", encoding="utf-8")
    assert "Монтаж щитов" in get_knowledge_index(profiles[1]).retrieve("щитов")
    assert "Монтаж кабеля" in electrician.retrieve("кабеля")  # Frozen original operation.


@pytest.mark.parametrize("kind", ["other_profile", "common_link", "leaf_link", "replaced_link"])
def test_shared_knowledge_rejects_links_to_other_sources(tmp_path, monkeypatch, kind):
    import kb_retrieval
    root = tmp_path / "profiles" / "electrician"
    root.mkdir(parents=True)
    common = write(tmp_path, "knowledge/candidate_facts.md", "- [FACT] Монтаж кабеля.")
    foreign = write(tmp_path, "profiles/foreign/knowledge/candidate_facts.md", "FOREIGN SECRET")
    link = root / "knowledge"
    link.symlink_to(common.parent, target_is_directory=True)
    if kind == "other_profile":
        link.unlink()
        link.symlink_to(foreign.parent, target_is_directory=True)
    elif kind == "common_link":
        common.unlink()
        common.parent.rmdir()
        common.parent.symlink_to(foreign.parent, target_is_directory=True)
    elif kind == "leaf_link":
        common.unlink()
        common.symlink_to(foreign)
    else:
        read_source = kb_retrieval._read_source
        def replace_before_read(*args):
            link.unlink()
            link.symlink_to(foreign.parent, target_is_directory=True)
            return read_source(*args)
        monkeypatch.setattr(kb_retrieval, "_read_source", replace_before_read)
    with pytest.raises(KnowledgeSourceError):
        get_knowledge_index(root)


def test_link_swap_after_validation_cannot_read_another_profile(tmp_path, monkeypatch):
    import kb_retrieval
    root = tmp_path / "profiles" / "electrician"
    root.mkdir(parents=True)
    common = write(tmp_path, "knowledge/candidate_facts.md", "COMMON CONFIRMED FACT")
    foreign = write(tmp_path, "profiles/foreign/knowledge/candidate_facts.md", "FOREIGN SECRET")
    link = root / "knowledge"
    link.symlink_to(common.parent, target_is_directory=True)
    stat = common.stat()
    validate = kb_retrieval._knowledge_root
    def swap_after_validation(profile_root):
        allowed = validate(profile_root)
        link.unlink()
        link.symlink_to(foreign.parent, target_is_directory=True)
        return allowed
    monkeypatch.setattr(kb_retrieval, "_knowledge_root", swap_after_validation)
    text = kb_retrieval._read_source(root, link / common.name,
                                     ("knowledge/candidate_facts.md", stat.st_mtime_ns, stat.st_size))
    assert text == "COMMON CONFIRMED FACT"
    assert "FOREIGN SECRET" not in text
