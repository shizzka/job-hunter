"""Remembered LLM vacancy evaluations, read only by the dry-run search.

A dry run neither applies nor marks vacancies as seen, so every dry run used to
re-evaluate everything and burned the shared free LLM quota (18.10: 215
evaluations in one run). Real searches always evaluate afresh against the
current resume and facts; they only feed this cache.
"""
from __future__ import annotations

import json
import os
import time

import config
import direction_history

CACHE_FILE = "evaluation_cache.json"
TTL_SECONDS = 3 * 24 * 3600
MAX_ENTRIES = 3000


def _path() -> str:
    return os.path.join(config.JOB_HUNTER_HOME, CACHE_FILE)


def _key(vacancy: dict) -> str:
    # A/B and closest variants must not reuse each other's verdict either.
    metadata = direction_history.runtime_metadata(vacancy)
    direction = metadata.get("direction") or ""
    variant = metadata.get("hh_resume_hash") or ""
    digest = metadata.get("variant_text_sha256") or ""
    return f"{vacancy.get('source') or 'hh'}:{vacancy.get('id') or ''}:{direction}:{variant}:{digest}"


def _read() -> dict:
    try:
        with open(_path(), encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def lookup(vacancy: dict) -> dict | None:
    if not vacancy.get("id"):
        return None
    bound = vacancy.get("_direction_selection") or {}
    if bound and bound.get("direction") != (getattr(config, "ACTIVE_DIRECTION", None) or {}).get("name"):
        return None
    data = _read()
    entry = data.get(_key(vacancy))
    if entry is None:
        # Before F15 the key ended at direction; preserve its original scope.
        direction = direction_history.runtime_metadata(vacancy).get("direction") or ""
        entry = data.get(f"{vacancy.get('source') or 'hh'}:{vacancy['id']}:{direction}")
    if not isinstance(entry, dict) or time.time() - float(entry.get("at") or 0) > TTL_SECONDS:
        return None
    evaluation = entry.get("evaluation")
    return dict(evaluation, evaluation_cached=True) if isinstance(evaluation, dict) else None


def store(vacancy: dict, evaluation: dict) -> None:
    """Never raises: the cache is an optimisation, not part of the search."""
    if not vacancy.get("id") or not isinstance(evaluation, dict) or evaluation.get("score") is None:
        return
    try:
        now = time.time()
        data = {key: value for key, value in _read().items()
                if isinstance(value, dict) and now - float(value.get("at") or 0) <= TTL_SECONDS}
        data[_key(vacancy)] = {"at": now, **direction_history.runtime_metadata(vacancy), "evaluation": {key: value for key, value in evaluation.items()
                                                         if key != "evaluation_cached"}}
        if len(data) > MAX_ENTRIES:
            data = dict(sorted(data.items(), key=lambda item: item[1]["at"])[-MAX_ENTRIES:])
        temp = _path() + ".tmp"
        with open(temp, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
        os.replace(temp, _path())
    except Exception:
        pass


def dry_run_cap() -> int:
    try:
        return max(0, int(os.getenv("DRY_RUN_MAX_LLM_EVALUATIONS", "30")))
    except ValueError:
        return 30


def reset(home, *, direction_scope="active", since=""):
    from datetime import datetime
    from state_store.protected import ProtectedJsonStore
    scope = direction_history.HistoryScope(home, direction_scope)
    cutoff = datetime.fromisoformat(since).timestamp() if since else 0
    store = ProtectedJsonStore(os.path.join(home, CACHE_FILE))
    def forget(data):
        for key in list(data):
            if scope.matches(data[key]) and float(data[key].get("at") or 0) >= cutoff:
                del data[key]
    store.update(forget)
