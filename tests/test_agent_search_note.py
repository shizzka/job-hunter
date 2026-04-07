import asyncio
from pathlib import Path

import agent


def test_format_no_new_vacancies_note_mentions_processed_results():
    text = agent._format_no_new_vacancies_note(
        {
            "hh": {
                "label": "hh.ru",
                "fetched": 50,
                "already_seen": 50,
            },
            "superjob": {
                "label": "SuperJob",
                "fetched": 12,
                "already_seen": 8,
            },
        }
    )

    assert text.startswith("Новых вакансий нет.")
    assert "hh.ru: просмотрено 50, уже обработано 50" in text
    assert "SuperJob: просмотрено 12, уже обработано 8" in text


def test_format_no_new_vacancies_note_falls_back_without_stats():
    assert agent._format_no_new_vacancies_note({}) == "Новых вакансий нет"


def test_save_autoapply_failure_snapshot_writes_screenshot_and_html(tmp_path, monkeypatch):
    class FakePage:
        async def screenshot(self, path: str, full_page: bool = False):
            Path(path).write_bytes(b"png")

        async def content(self) -> str:
            return "<html>failure</html>"

    monkeypatch.setattr(agent.config, "HH_STATE_DIR", str(tmp_path))

    saved = asyncio.run(
        agent._save_autoapply_failure_snapshot("hh", "vacancy/42", FakePage())
    )

    assert Path(saved["screenshot"]).is_file()
    assert Path(saved["html"]).is_file()
    assert Path(saved["screenshot"]).parent == tmp_path
    assert Path(saved["html"]).read_text(encoding="utf-8") == "<html>failure</html>"
