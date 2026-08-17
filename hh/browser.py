from __future__ import annotations

import json
import os

import config


def _ensure_dirs():
    os.makedirs(os.path.dirname(config.HH_COOKIES_FILE), exist_ok=True)
    os.makedirs(config.HH_STATE_DIR, exist_ok=True)


def _load_cookies() -> list[dict] | None:
    if os.path.exists(config.HH_COOKIES_FILE):
        with open(config.HH_COOKIES_FILE) as f:
            return json.load(f)
    return None


def _save_cookies(cookies: list[dict]):
    _ensure_dirs()
    with open(config.HH_COOKIES_FILE, "w") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
