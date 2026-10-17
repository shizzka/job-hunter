import telegram_access as access


def test_admin_can_act_as_client_only_when_dev_file_is_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(access, "load_registry", lambda: {"users": [
        {"user_id": 1, "role": access.ROLE_ADMIN, "profile": "qa", "enabled": True},
        {"user_id": 2, "role": access.ROLE_USER, "profile": "client_2", "enabled": True}]})
    monkeypatch.delenv("JOB_HUNTER_IMPERSONATION_FILE", raising=False)
    try:
        access.set_impersonation(1, "marina-dev")
        assert False, "must be refused outside the dev bot"
    except RuntimeError:
        pass
    monkeypatch.setenv("JOB_HUNTER_IMPERSONATION_FILE", str(tmp_path / "imp.json"))
    access.set_impersonation(1, "marina-dev")
    acting = access.resolve_user(1)
    assert acting["role"] == access.ROLE_USER and acting["profile"] == "marina-dev" and acting["impersonating"]
    access.set_impersonation(2, "qa")  # a client entry is ignored: only admins can act as someone
    assert access.resolve_user(2)["role"] == access.ROLE_USER and "impersonating" not in access.resolve_user(2)
    access.set_impersonation(1, None)
    assert access.resolve_user(1)["role"] == access.ROLE_ADMIN
