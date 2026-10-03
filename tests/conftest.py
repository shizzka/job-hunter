"""Keep ordinary tests offline and mocked analytics out of runtime files."""
import socket

import pytest
import config


@pytest.fixture(autouse=True)
def deny_test_network(monkeypatch):
    """Ordinary tests use fake transports, never real IP connections.

    This guard covers this pytest process only. Subprocess tests must supply
    isolated environments and synthetic fixtures separately.
    """
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded(method):
        def connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                raise AssertionError("Real network connections are forbidden in ordinary tests")
            return method(sock, address)
        return connect

    monkeypatch.setattr(socket.socket, "connect", guarded(original_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(original_connect_ex))


@pytest.fixture(autouse=True)
def isolated_default_analytics_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ANALYTICS_EVENTS_FILE', str(tmp_path / 'default_events.jsonl'))
    monkeypatch.setattr(config, 'ANALYTICS_STATE_FILE', str(tmp_path / 'default_state.json'))
