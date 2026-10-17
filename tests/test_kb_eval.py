import json

import pytest

from scripts import kb_eval


def test_question_groups_and_normalization(tmp_path):
    source = tmp_path / "questions.txt"
    source.write_text('# local\nExample? → {"all": ["ＦＯＯ", "two words"], "any": ["bar", "baz"]}\n')
    question, = kb_eval.read_questions(source)
    assert kb_eval.score_context(question, "foo TWO\nWORDS baz")["hit"]
    assert not kb_eval.score_context(question, "foo two words")["hit"]
    assert not kb_eval.score_context(question, "foo baz")["hit"]


@pytest.mark.parametrize("labels", [{}, {"all": []}, {"all": "secret"}, {"any": [""]}, {"bad": ["secret"]}])
def test_invalid_labels_do_not_disclose_input(tmp_path, labels):
    source = tmp_path / "questions.txt"
    source.write_text("sensitive query → " + json.dumps(labels))
    with pytest.raises(ValueError, match="^Invalid question format at line 1$"):
        kb_eval.read_questions(source)


def test_evaluation_counts_and_details_are_content_private(tmp_path):
    calls = []

    def legacy(**kwargs):
        calls.append(kwargs)
        return "alpha"

    def retrieve(query, **kwargs):
        calls.append(kwargs)
        return "beta" if query == "private second" else "alpha"

    questions = [kb_eval.Question("private first", ("alpha",)), kb_eval.Question("private second", ("beta",))]
    result = kb_eval.evaluate(questions, tmp_path, legacy, retrieve, details=True)
    assert result["legacy"]["hits"] == 1
    assert result["retrieval"]["hits"] == 2
    assert result["retrieval"]["hit_rate"] == 1
    assert result["details"][1] == {"id": "q002", "legacy": False, "legacy_same_budget": False, "retrieval": True}
    assert result["budgets"] == {"legacy": 6000, "legacy_same_budget": 12000, "retrieval": 12000}
    assert len(calls) == 4
    assert calls[0] == {"limit_chars": 6000, "profile_dir": str(tmp_path)}
    assert all(call == {"limit_chars": 12000, "profile_dir": str(tmp_path)} for call in calls[1:])
    output = json.dumps(result)
    assert not any(value in output for value in ["private", "alpha", "beta"])


def test_cli_excludes_local_labels_from_both_builders(tmp_path, monkeypatch, capsys):
    import config
    import kb_retrieval
    import prompt_blocks

    knowledge = tmp_path / "profiles" / "qa" / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge.parent / "facts.json").write_text('{"confirmed":{"example":"synthetic"}}')
    (knowledge / "source.md").write_text("evidence")
    (knowledge / "kb_eval_questions.txt").write_text("default labels")
    custom = knowledge / "custom.txt"
    custom.write_text('private query → {"all":["evidence"]}')
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

    def build(**kwargs):
        snapshot = kb_eval.Path(kwargs["profile_dir"]) / "knowledge"
        assert sorted(path.name for path in snapshot.iterdir()) == ["source.md"]
        assert json.loads((snapshot.parent / "facts.json").read_text())["confirmed"]["example"] == "synthetic"
        return (snapshot / "source.md").read_text()

    monkeypatch.setattr(prompt_blocks, "build_knowledge_base_block", build)
    monkeypatch.setattr(kb_retrieval, "retrieve_knowledge", lambda query, **kwargs: build(**kwargs))
    assert kb_eval.main(["--profile", "qa", "--questions", str(custom)]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["retrieval"]["hits"] == 1
    assert "private" not in output


def test_explicit_budget_separates_capacity_from_retrieval(tmp_path):
    question = kb_eval.Question("synthetic", ("evidence",))

    def legacy(**kwargs):
        return "evidence" if kwargs["limit_chars"] > 6000 else ""

    result = kb_eval.evaluate([question], tmp_path, legacy, lambda query, **kwargs: "evidence", budget=9000)
    assert result["legacy"]["hits"] == 0
    assert result["legacy_same_budget"]["hits"] == result["retrieval"]["hits"] == 1
    assert result["budgets"]["retrieval"] == 9000


@pytest.mark.parametrize("linked_source", ["knowledge", "knowledge/source.md", "knowledge/synonyms.txt", "facts.json", "candidate_interview.json"])
def test_cli_rejects_linked_sources_before_copy(tmp_path, monkeypatch, capsys, linked_source):
    import config

    profile = tmp_path / "profiles" / "qa"
    knowledge = profile / "knowledge"
    knowledge.mkdir(parents=True)
    questions = tmp_path / "questions.txt"
    questions.write_text('synthetic? → {"all":["evidence"]}')
    external = tmp_path / "external"
    if linked_source == "knowledge":
        knowledge.rmdir()
        external.mkdir()
        knowledge.symlink_to(external, target_is_directory=True)
    else:
        external.write_text("sensitive external source")
        (profile / linked_source).symlink_to(external)
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

    def forbidden_copy(*args, **kwargs):
        pytest.fail("Symlinked source must never be copied/read")

    monkeypatch.setattr(kb_eval.shutil, "copyfile", forbidden_copy)
    assert kb_eval.main(["--profile", "qa", "--questions", str(questions)]) == 1
    output = capsys.readouterr()
    assert "Evaluation failed (ValueError)" in output.err
    assert "sensitive" not in output.err
