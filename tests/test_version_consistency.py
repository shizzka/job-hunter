from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _version() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def test_public_version_is_consistent():
    version = _version()
    assert version

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_ru = (ROOT / "README.ru.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    marker = f"`v{version}`"
    assert f"**Current version:** {marker}" in readme
    assert f"**Текущая версия:** {marker}" in readme_ru
    assert f"## v{version} " in changelog
