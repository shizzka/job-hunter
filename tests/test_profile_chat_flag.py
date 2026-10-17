import config
import profile


def test_profile_can_switch_chat_responder_off_and_default_is_restored(monkeypatch):
    monkeypatch.setattr(profile, "_default_chat_responder", None)
    monkeypatch.setattr(config, "HH_CHAT_RESPONDER_ENABLED", True)
    p = profile.load_default_profile()
    profile._apply_env_overrides(p, {"HH_CHAT_RESPONDER_ENABLED": "0"})
    assert p.chat_responder_enabled is False
    profile._patch_config(p)
    assert config.HH_CHAT_RESPONDER_ENABLED is False
    other = profile.load_default_profile()
    other.chat_responder_enabled = None
    profile._patch_config(other)
    assert config.HH_CHAT_RESPONDER_ENABLED is True
