import json
import stat

import geekjob_client
import habr_career_client
import superjob_client


def test_source_credentials_are_saved_atomically_with_private_permissions(tmp_path, monkeypatch):
    geekjob_path = tmp_path / "geekjob" / "cookies.json"
    habr_path = tmp_path / "habr" / "cookies.json"
    superjob_cookies_path = tmp_path / "superjob" / "cookies.json"
    superjob_auth_path = tmp_path / "superjob" / "auth.json"

    monkeypatch.setattr(geekjob_client.config, "GEEKJOB_COOKIES_FILE", str(geekjob_path))
    monkeypatch.setattr(habr_career_client.config, "HABR_COOKIES_FILE", str(habr_path))
    monkeypatch.setattr(superjob_client.config, "SUPERJOB_COOKIES_FILE", str(superjob_cookies_path))
    monkeypatch.setattr(superjob_client.config, "SUPERJOB_AUTH_FILE", str(superjob_auth_path))
    monkeypatch.setattr(geekjob_client.config, "HH_STATE_DIR", str(tmp_path / "state"))

    cookies = [{"name": "session", "value": "secret"}]
    auth = {"access_token": "secret-token"}
    geekjob_client._save_cookies(cookies)
    habr_career_client._save_cookies(cookies)
    superjob_client._save_cookies(cookies)
    superjob_client._save_auth_file(auth)

    for path, expected in (
        (geekjob_path, cookies),
        (habr_path, cookies),
        (superjob_cookies_path, cookies),
        (superjob_auth_path, auth),
    ):
        assert json.loads(path.read_text(encoding="utf-8")) == expected
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert list(path.parent.glob(f".{path.name}.*.tmp")) == []
