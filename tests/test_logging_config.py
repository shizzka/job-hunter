import pytest

import agent
import telegram_bot


@pytest.mark.parametrize("module", (agent, telegram_bot))
def test_configure_logging_skips_handler_build_when_root_is_configured(module, monkeypatch):
    root = module.logging.getLogger()
    handler = module.logging.NullHandler()
    root.addHandler(handler)

    def unexpected_call(*args, **kwargs):
        raise AssertionError("logging handlers must not be built or installed")

    monkeypatch.setattr(module.logging, "basicConfig", unexpected_call)
    monkeypatch.setattr(module, "_build_logging_handlers", unexpected_call)

    try:
        module._configure_logging()
    finally:
        root.removeHandler(handler)


@pytest.mark.parametrize("module", (agent, telegram_bot))
def test_background_logging_uses_file_handler_without_duplicate_stream(monkeypatch, module):
    monkeypatch.setattr(module.sys.stdout, "isatty", lambda: False)
    handlers = module._build_logging_handlers()
    try:
        assert not any(
            isinstance(handler, module.logging.StreamHandler)
            and not isinstance(handler, module.logging.FileHandler)
            for handler in handlers
        )
        assert any(isinstance(handler, module.logging.FileHandler) for handler in handlers)
    finally:
        for handler in handlers:
            handler.close()
