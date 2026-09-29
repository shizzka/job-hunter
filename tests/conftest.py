"""Keep default analytics writes from mocked integrations out of runtime files."""
import pytest
import config


@pytest.fixture(autouse=True)
def isolated_default_analytics_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ANALYTICS_EVENTS_FILE', str(tmp_path / 'default_events.jsonl'))
    monkeypatch.setattr(config, 'ANALYTICS_STATE_FILE', str(tmp_path / 'default_state.json'))
