import asyncio
from types import SimpleNamespace

import question_memory as qm


def test_normalize_ignores_case_punctuation_and_parentheses():
    assert qm.normalize("Есть ли у тебя приписное/военник? (вопрос для мужчин)") == qm.normalize("есть ли у тебя приписное военник")


def test_remember_lookup_and_choice_mapping(tmp_path):
    qm.remember(str(tmp_path), "Работал(-а) ли ты с SQL?", "Есть базовые знания", control="radio", labels=["Есть базовые знания"])
    item = qm.lookup(str(tmp_path), "работал ли ты с sql")
    assert item["answer"] == "Есть базовые знания"
    options = [{"index": 0, "label": "Нет"}, {"index": 1, "label": "Есть базовые знания"}, {"index": 2, "label": "Эксперт"}]
    assert qm.choice_indices(item, options) == [1]
    assert qm.choice_indices(item, [{"index": 0, "label": "Да"}]) == []


def test_pending_roundtrip(tmp_path):
    qid = qm.create_pending(str(tmp_path), question="Военник?", control="radio", options=["Да", "Нет"],
                            token="tok", profile_name="qa")
    assert qm.peek_pending(str(tmp_path), qid)["token"] == "tok"
    assert qm.pop_pending(str(tmp_path), qid)["options"] == ["Да", "Нет"]
    assert qm.pop_pending(str(tmp_path), qid) is None


def test_forms_use_remembered_answer_instead_of_asking(tmp_path, monkeypatch):
    from hh import forms
    qm.remember(str(tmp_path), "Есть ли военный билет?", "Да", control="radio", labels=["Да"])
    qm.remember(str(tmp_path), "Почему хотите к нам?", "Нравится продукт.", control="textarea")
    filled, submitted = [], []

    class Session:
        _cookie_paths = None
        _approved_hh_payload = None
        _page = SimpleNamespace(wait_for_timeout=lambda ms: asyncio.sleep(0))
        async def _inspect_employer_questions(self):
            return {"page_text": "", "unsupported_fields": 0, "fields": [
                {"field_id": "a", "control": "radio", "question_text": "Есть ли военный билет?",
                 "options": [{"index": 0, "label": "Да"}, {"index": 1, "label": "Нет"}]},
                {"field_id": "b", "control": "textarea", "question_text": "Почему хотите к нам?"}]}
        async def _answer_choice_with_llm(self, *a):
            raise AssertionError("LLM must not be asked for a remembered answer")
        async def _answer_question_with_llm(self, *a):
            raise AssertionError("LLM must not be asked for a remembered answer")
        async def _fill_employer_question_answers(self, answers):
            filled.extend(answers)
            return {"filled": len(answers)}
        async def _submit_employer_questions(self, **kwargs):
            submitted.append(True)
            return True
        async def _detect_anti_bot_kind(self):
            return ""
        async def _handle_anti_bot_with_solver(self, kind, stage=""):
            return ""
        def __getattr__(self, name):  # post-submit success probes are irrelevant here
            async def stub(*a, **k):
                return True
            return stub

    settings = SimpleNamespace(HH_AUTO_ANSWER_SIMPLE_QUESTIONS=True, HH_AUTO_ANSWER_MAX_QUESTIONS=20,
                               HH_AUTO_ANSWER_SALARY_TEXT="", HH_AUTO_ANSWER_SALARY_NUMBER="",
                               VACANCY_FILTER_POLICY="qa", JOB_HUNTER_HOME=str(tmp_path))
    result = asyncio.run(forms.try_auto_answer_questions(
        Session(), settings=settings, load_resume_text=lambda: "", anti_bot_message=lambda *a: ""))
    assert {a["field_id"] for a in filled} == {"a", "b"} and submitted
    assert next(a for a in filled if a["field_id"] == "a")["selected_indices"] == [0]


def test_unanswerable_question_is_exposed_for_the_user(tmp_path):
    from hh import forms

    class Session:
        _cookie_paths = None
        async def _inspect_employer_questions(self):
            return {"page_text": "", "unsupported_fields": 0, "fields": [
                {"field_id": "a", "control": "radio", "question_text": "Как относитесь к зарплатной карте банка?",
                 "options": [{"index": 0, "label": "Не против"}, {"index": 1, "label": "Против"}]}]}
        async def _answer_choice_with_llm(self, *a):
            return None

    settings = SimpleNamespace(HH_AUTO_ANSWER_SIMPLE_QUESTIONS=True, HH_AUTO_ANSWER_MAX_QUESTIONS=20,
                               HH_AUTO_ANSWER_SALARY_TEXT="", HH_AUTO_ANSWER_SALARY_NUMBER="",
                               VACANCY_FILTER_POLICY="qa", JOB_HUNTER_HOME=str(tmp_path))
    result = asyncio.run(forms.try_auto_answer_questions(
        Session(), settings=settings, load_resume_text=lambda: "", anti_bot_message=lambda *a: ""))
    assert not result["ok"]
    assert result["pending_question"] == {"question_text": "Как относитесь к зарплатной карте банка?",
                                          "control": "radio", "options": ["Не против", "Против"]}


def test_answers_and_pending_cards_are_scoped_by_direction_and_resume(tmp_path):
    home = str(tmp_path)
    question = "Сколько лет опыта в профессии?"
    electric = {"direction": "electric", "resume_id": "electric-resume"}
    qa = {"direction": "qa", "resume_id": "qa-resume"}
    qm.remember(home, question, "Пять лет", **electric)
    assert qm.lookup(home, question, **electric)["answer"] == "Пять лет"
    assert qm.lookup(home, question, **qa) is None
    assert qm.lookup(home, question, direction="electric", resume_id="another-resume") is None
    qm.remember(home, question, "Один год", **qa)
    assert qm.lookup(home, question, **electric)["answer"] == "Пять лет"
    qid = qm.create_pending(home, question=question, control="text", options=[], token="t", profile_name="p",
                            url="https://hh.ru/vacancy/1", **electric)
    assert qm.peek_pending(home, qid)["direction"] == "electric"
    assert qm.find_pending(home, question=question, url="https://hh.ru/vacancy/1", **electric) == qid
    assert not qm.find_pending(home, question=question, url="https://hh.ru/vacancy/1", **qa)


def test_legacy_answer_without_ownership_requires_confirmation(tmp_path):
    import json
    question = "Сколько лет опыта в профессии?"
    (tmp_path / qm.MEMORY_FILE).write_text(json.dumps({qm.normalize(question): {"answer": "Пять лет"}}))
    assert qm.lookup(str(tmp_path), question) is None
    assert qm.lookup(str(tmp_path), question, direction="qa", resume_id="qa-resume") is None



def test_questionnaire_does_not_reuse_another_professions_answer(tmp_path):
    from hh import forms
    question = "Сколько лет опыта в этой профессии?"
    qm.remember(str(tmp_path), question, "Пять лет", direction="electric", resume_id="electric-resume")
    class Session:
        _approved_hh_payload = {"resume_id": "qa-resume"}
        async def _inspect_employer_questions(self):
            return {"page_text": "", "unsupported_fields": 0, "fields": [
                {"field_id": "a", "control": "textarea", "question_text": question}]}
        async def _answer_question_with_llm(self, *args, **kwargs):
            return None
    settings = SimpleNamespace(HH_AUTO_ANSWER_SIMPLE_QUESTIONS=True, HH_AUTO_ANSWER_MAX_QUESTIONS=20,
        HH_AUTO_ANSWER_SALARY_TEXT="", HH_AUTO_ANSWER_SALARY_NUMBER="", VACANCY_FILTER_POLICY="generic",
        JOB_HUNTER_HOME=str(tmp_path), ACTIVE_DIRECTION={"name": "qa"})
    result = asyncio.run(forms.try_auto_answer_questions(Session(), settings=settings,
        load_resume_text=lambda: "", anti_bot_message=lambda *a: ""))
    assert not result['ok'] and result['pending_question']['question_text'] == question
