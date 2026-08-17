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
