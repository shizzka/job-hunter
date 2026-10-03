"""Advisory resume analysis must not expose failures or partial model output."""
import asyncio
from types import SimpleNamespace

import pytest
import resume_analyzer as analyzer


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(analyzer, '_load_prompt', lambda: ('Analyze', '{resume}'))


def test_analysis_does_not_return_or_log_raw_provider_exception(isolated, monkeypatch, caplog):
    async def create(**kwargs): raise RuntimeError('synthetic-private-resume-and-key')
    monkeypatch.setattr(analyzer, '_get_client', lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    output = asyncio.run(analyzer.analyze_resume('synthetic resume'))
    assert 'synthetic-private-resume-and-key' not in output + caplog.text
    assert 'Ошибка анализа' in output


@pytest.mark.parametrize('finish_reason', ['length', 'content_filter', 'tool_calls'])
def test_incomplete_analysis_is_not_successful_advice(isolated, monkeypatch, finish_reason):
    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason,
            message=SimpleNamespace(content='partial unverified advice'))])
    monkeypatch.setattr(analyzer, '_get_client', lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    output = asyncio.run(analyzer.analyze_resume('synthetic resume'))
    assert 'partial unverified advice' not in output and 'Ошибка анализа' in output


def test_bad_prompt_template_returns_controlled_error(isolated, monkeypatch):
    monkeypatch.setattr(analyzer, '_load_prompt', lambda: ('Analyze', '{unexpected_placeholder}'))
    output = asyncio.run(analyzer.analyze_resume('synthetic resume'))
    assert 'Ошибка анализа' in output


def test_invalid_resume_utf8_returns_controlled_error(tmp_path):
    path = tmp_path / 'resume.md'; path.write_bytes(b'\xff')
    output = asyncio.run(analyzer.analyze_resume_file(str(path)))
    assert 'Ошибка анализа' in output and path.read_bytes() == b'\xff'


def test_valid_advisory_publication_is_private_and_preserves_resume(tmp_path):
    from state_store.resume_analysis import AnalysisPublication
    resume, output = tmp_path / 'resume.md', tmp_path / 'analysis.md'
    resume.write_text('Synthetic source')
    publication = AnalysisPublication(resume, output)
    assert publication.publish('Synthetic valid advice')
    assert resume.read_text() == 'Synthetic source'
    assert output.read_text() == 'Synthetic valid advice'
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('changed', ['resume', 'output'])
def test_stale_publication_never_clobbers_changed_source_or_output(tmp_path, changed):
    from state_store.resume_analysis import AnalysisPublication
    resume, output = tmp_path / 'resume.md', tmp_path / 'analysis.md'
    resume.write_text('Original source'); output.write_text('Original advice')
    publication = AnalysisPublication(resume, output)
    (resume if changed == 'resume' else output).write_text('New owner bytes')
    before = (resume.read_bytes(), output.read_bytes())
    with pytest.raises(RuntimeError):
        publication.publish('Stale advice')
    assert (resume.read_bytes(), output.read_bytes()) == before


@pytest.mark.parametrize('bad', ['', 'Ошибка анализа: synthetic', 'Резюме пустое', 'Файл не найден'])
def test_failed_analysis_does_not_replace_existing_advice(tmp_path, bad):
    from state_store.resume_analysis import AnalysisPublication
    resume, output = tmp_path / 'resume.md', tmp_path / 'analysis.md'
    resume.write_text('Original source'); output.write_text('Original advice')
    publication = AnalysisPublication(resume, output)
    assert publication.publish(bad) is False
    assert output.read_text() == 'Original advice'


def test_two_publications_from_same_revision_cannot_overwrite_each_other(tmp_path):
    from state_store.resume_analysis import AnalysisPublication
    resume, output = tmp_path / 'resume.md', tmp_path / 'analysis.md'
    resume.write_text('Original source')
    first, second = AnalysisPublication(resume, output), AnalysisPublication(resume, output)
    assert first.publish('First approved advice')
    with pytest.raises(RuntimeError):
        second.publish('Second stale advice')
    assert output.read_text() == 'First approved advice'


def test_analysis_cancellation_propagates_without_result(isolated, monkeypatch):
    async def create(**kwargs): raise asyncio.CancelledError
    monkeypatch.setattr(analyzer, '_get_client', lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(analyzer.analyze_resume('Synthetic resume'))
