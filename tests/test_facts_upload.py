import os

import pytest

from telegram_app import facts_upload


def test_summary_counts_sections_and_tags():
    info = facts_upload.summarize("# Опыт\n- [FACT] Банк\n## Запреты\n- [DO_NOT_CLAIM] Python\n")
    assert info["headings"] == ["Опыт", "Запреты"] and info["tags"]["FACT"] == 1 and info["tags"]["DO_NOT_CLAIM"] == 1


def test_save_keeps_backup_and_writes_atomically(tmp_path):
    knowledge = tmp_path / "knowledge"
    facts_upload.save(str(knowledge), "old")
    facts_upload.save(str(knowledge), "new facts")
    assert (knowledge / "candidate_facts.md").read_text() == "new facts\n"
    assert any(name.startswith("candidate_facts.md.bak-") for name in os.listdir(knowledge))


def test_shared_symlinked_knowledge_is_refused(tmp_path):
    real = tmp_path / "common"
    real.mkdir()
    link = tmp_path / "knowledge"
    link.symlink_to(real)
    with pytest.raises(ValueError):
        facts_upload.save(str(link), "x")
