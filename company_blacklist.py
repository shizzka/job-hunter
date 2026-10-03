"""Profile-local employer exclusions, checked again immediately before applying."""
import json
import re
from pathlib import Path

import config
from state_store.json_store import atomic_write_json, file_lock


def normalize(company: str) -> str:
    words = re.findall(r"[^\W_]+", str(company or "").casefold().replace("ё", "е"))
    while words and words[0] in {"ооо", "пао", "оао", "зао", "ао"}:
        words.pop(0)
    return " ".join(words)


def _path(profile_name=None):
    if profile_name is not None:
        import profile as profile_mod
        home = profile_mod.load_profile(profile_name).home_dir
    else:
        home = config.JOB_HUNTER_HOME
    return Path(home) / "company_blacklist.json"


def list_companies(profile_name=None):
    return _read_companies(_path(profile_name))


def _read_companies(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(data, list) or any(not isinstance(x, str) for x in data):
        raise ValueError("Invalid company blacklist")
    return data


def is_blocked(company, profile_name=None):
    key = normalize(company)
    return bool(key) and any(normalize(x) == key for x in list_companies(profile_name))


def set_blocked(company, blocked=True, profile_name=None):
    company = str(company or "").strip()
    key = normalize(company)
    if not key or len(company) > 200 or "\n" in company:
        raise ValueError("Введите название одной компании (до 200 символов).")
    path = _path(profile_name)
    with file_lock(path, lock_path=path.with_suffix(".lock")):
        companies = [x for x in _read_companies(path) if normalize(x) != key]
        if blocked:
            companies.append(company)
        atomic_write_json(path, sorted(companies, key=str.casefold))
