"""Only synthetic letters in temporary profile directories are used here."""
import builtins
import logging

import pytest

import config
import matcher
import profile


@pytest.fixture
def letter_path(tmp_path):
    return tmp_path / "cover_letter_fallback.md"


def legacy_letter(style):
    subject = "другой вариант резюме" if style == "short_retry" else "резюме"
    return (
        f"Здравствуйте! Направляю {subject} на вашу вакансию. "
        "Подробности моего опыта и навыков указаны в резюме. "
        "Если профиль подходит, можно обсудить задачи и формат работы."
    )


@pytest.mark.parametrize("style", ["", "short_retry"])
def test_profile_letter_is_stripped_and_reloaded(letter_path, style, caplog):
    letter_path.write_text("  Вымышленное письмо А.\nВторая строка.\n", encoding="utf-8")
    assert matcher._fallback_cover_letter({}, cover_style=style) == "Вымышленное письмо А.\nВторая строка."
    letter_path.write_text("Вымышленное письмо Б.", encoding="utf-8")
    assert matcher._fallback_cover_letter({}, cover_style=style) == "Вымышленное письмо Б."
    letter_path.unlink()
    assert matcher._fallback_cover_letter({}, cover_style=style) == legacy_letter(style)
    assert not caplog.records


@pytest.mark.parametrize("style", ["", "short_retry"])
def test_missing_file_keeps_exact_legacy_template(letter_path, style, caplog):
    assert not letter_path.exists()
    assert matcher._fallback_cover_letter({}, cover_style=style) == legacy_letter(style)
    assert not caplog.records


@pytest.mark.parametrize("style", ["", "short_retry"])
@pytest.mark.parametrize("payload,reason", [
    (b"", "empty"),
    (b" \n ", "empty"),
    (b"x" * 1501, "too_long length=1501"),
    (b"SYNTHETIC_PRIVATE\xff\xfe", "invalid_utf8"),
    (b"SYNTHETIC_PRIVATE\x00", "control_characters"),
    (b"\tSYNTHETIC_PRIVATE", "control_characters"),
    (b"SYNTHETIC_PRIVATE\r\n", "control_characters"),
    (b"SYNTHETIC_PRIVATE\x7f", "control_characters"),
    ("SYNTHETIC_PRIVATE\u0085".encode("utf-8"), "control_characters"),
])
def test_invalid_file_warns_once_without_contents(letter_path, style, payload, reason, caplog):
    letter_path.write_bytes(payload)
    with caplog.at_level(logging.WARNING, logger="matcher"):
        assert matcher._fallback_cover_letter({}, cover_style=style) == legacy_letter(style)
    assert [record.getMessage() for record in caplog.records] == [
        f"Profile fallback letter rejected: {reason}",
    ]
    assert "SYNTHETIC_PRIVATE" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_unreadable_file_does_not_log_exception_contents(letter_path, monkeypatch, caplog):
    real_open = builtins.open

    def denied(path, *args, **kwargs):
        if str(path) == str(letter_path):
            raise PermissionError("SYNTHETIC_PRIVATE_EXCEPTION")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied)
    assert matcher._fallback_cover_letter({}) == legacy_letter("")
    assert [record.getMessage() for record in caplog.records] == [
        "Profile fallback letter rejected: unreadable",
    ]
    assert "SYNTHETIC_PRIVATE" not in caplog.text
    assert caplog.records[0].exc_info is None


def test_directory_instead_of_file_is_unreadable(letter_path, caplog):
    letter_path.mkdir()
    assert matcher._fallback_cover_letter({}) == legacy_letter("")
    assert [record.getMessage() for record in caplog.records] == [
        "Profile fallback letter rejected: unreadable",
    ]


def test_1500_unicode_characters_are_allowed_after_strip(letter_path, caplog):
    text = "я" * 1500
    letter_path.write_text(f" \n{text}\n ", encoding="utf-8")
    assert matcher._fallback_cover_letter({}) == text
    assert not caplog.records


@pytest.mark.parametrize("style", ["", "short_retry"])
def test_named_profile_activation_selects_own_letter(tmp_path, monkeypatch, style):
    # Activation patches config globally; restore every setting afterwards.
    for key, value in vars(config).copy().items():
        if key.isupper():
            monkeypatch.setattr(config, key, value)
    monkeypatch.delenv("JOB_HUNTER_HOME", raising=False)
    monkeypatch.setattr(profile, "_profiles_root_home", str(tmp_path))
    monkeypatch.setattr(profile, "_active_profile", None)
    monkeypatch.setattr(profile, "_default_candidate_settings", None)
    monkeypatch.setattr(profile, "_lock_fd", None)
    for name in ("qa", "qa-dev"):
        directory = tmp_path / "profiles" / name
        directory.mkdir(parents=True)
        (directory / "profile.env").write_text("# synthetic profile\n", encoding="utf-8")
        (directory / "cover_letter_fallback.md").write_text(f"Вымышленное письмо {name}.", encoding="utf-8")
    for name in ("qa", "qa-dev", "qa"):
        active = profile.activate_no_lock(name)
        assert config.JOB_HUNTER_HOME == active.home_dir == str(tmp_path / "profiles" / name)
        assert matcher._fallback_cover_letter({}, cover_style=style) == f"Вымышленное письмо {name}."
