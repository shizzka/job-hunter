"""Owner-confirmed application bindings; tokens refer to one HH thread only."""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

from state_store.protected import ProtectedJsonStore

REVIEW_AGE = 7 * 24 * 3600
SELECTION_AGE = 15 * 60


def _valid(value):
    return (isinstance(value, dict) and set(value) == {"reviews", "selections", "bindings"}
            and all(isinstance(value[key], dict) for key in value)
            and all(isinstance(item, dict) for section in value.values() for item in section.values()))


def _empty():
    return {"reviews": {}, "selections": {}, "bindings": {}}


def _path(home):
    return Path(home) / "chat_owner_review.json"


def _read(home):
    # Ownership lookup is read-only, including when no review has ever existed.
    try:
        with _path(home).open(encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return _empty()
    if not _valid(value):
        raise ValueError("Invalid chat ownership review state")
    return value


def _store(home):
    return ProtectedJsonStore(_path(home), default_factory=_empty, validator=_valid)


def _review(value, token):
    review = value["reviews"].get(token)
    if (not review or review.get("used") or not isinstance(review.get("created_at"), (int, float))
            or not 0 <= time.time() - review["created_at"] < REVIEW_AGE):
        raise ValueError("Карточка устарела. Откройте свежую карточку чата.")
    return review


def read_review(home, token):
    return _review(_read(home), token)


def create_review(home, chat_id, message_id, profile_name):
    if not str(chat_id) or not str(profile_name):
        raise ValueError("Не удалось определить чат и профиль.")
    token = secrets.token_hex(6)
    def save(value):
        # Expired UI tokens may go; confirmed bindings remain until contradicted.
        now = time.time()
        value["reviews"] = {key: row for key, row in value["reviews"].items()
                            if now - row.get("created_at", 0) < REVIEW_AGE}
        value["selections"] = {key: row for key, row in value["selections"].items()
                               if now - row.get("created_at", 0) < SELECTION_AGE}
        value["reviews"][token] = {"chat_id": str(chat_id), "message_id": str(message_id),
                                    "profile_name": str(profile_name), "created_at": now, "used": False}
    _store(home).update(save)
    return token


def create_selection(home, review_token, options, actor_id):
    if not options or type(actor_id) is not int or actor_id <= 0:
        raise ValueError("Не удалось определить владельца или резюме.")
    token = secrets.token_hex(6)
    def save(value):
        _review(value, review_token)
        value["selections"][token] = {"review_token": review_token, "options": options,
                                       "actor_id": actor_id, "created_at": time.time()}
    _store(home).update(save)
    return token


def read_selection(home, token, actor_id):
    value = _read(home)
    selection = value["selections"].get(token)
    if (type(actor_id) is not int or actor_id <= 0 or not selection or selection.get("actor_id") != actor_id
            or not 0 <= time.time() - selection.get("created_at", 0) < SELECTION_AGE):
        raise ValueError("Выбор недоступен. Откройте карточку чата заново.")
    _review(value, selection["review_token"])
    return selection


def confirmed_binding(home, chat_id):
    return _read(home)["bindings"].get(str(chat_id))


def confirm_selection(home, selection_token, index, actor_id, target_home):
    """Publish only the exact resume/direction the authenticated owner selected."""
    import chat_context as cc
    selection = read_selection(home, selection_token, actor_id)
    if type(index) is not int or not 0 <= index < len(selection["options"]):
        raise ValueError("Резюме недоступно. Откройте выбор заново.")
    option = selection["options"][index]
    review = read_review(home, selection["review_token"])
    target_home = os.path.abspath(target_home)
    resume_id, direction = str(option.get("resume_id") or ""), option.get("direction", "")
    if (option.get("stage") != "resume" or not resume_id
            or cc.profile_resumes(target_home).get(resume_id, (None,))[0] != direction):
        raise ValueError("Резюме или направление изменилось. Откройте выбор заново.")
    def check_target(profile_home, previous):
        observed = cc.chat_resume_id(profile_home, review["chat_id"])
        if observed and observed != resume_id:
            raise ValueError("Для этого отклика указано другое резюме. Проверьте отклик в чате.")
        if previous and (previous.get("resume_id") != resume_id
                         or previous.get("profile_home") != target_home
                         or previous.get("direction") != direction
                         or previous.get("source_home") != os.path.abspath(home)):
            raise ValueError("Для этого чата уже выбрано другое резюме. Проверьте отклик в чате.")
    binding = {"resume_id": resume_id, "direction": direction, "profile_home": target_home,
               "profile_name": option["profile_name"], "source_home": os.path.abspath(home),
               "actor_id": actor_id, "chosen_at": time.time()}
    for profile_home in {os.path.abspath(home), target_home}:
        check_target(profile_home, confirmed_binding(profile_home, review["chat_id"]))
    def publish(value):
        _review(value, selection["review_token"])
        check_target(os.path.abspath(home), value["bindings"].get(review["chat_id"]))
        value["bindings"][review["chat_id"]] = binding
        value["reviews"][selection["review_token"]]["used"] = True
    _store(home).update(publish)
    if target_home != os.path.abspath(home):
        def publish_target(value):
            check_target(target_home, value["bindings"].get(review["chat_id"]))
            value["bindings"][review["chat_id"]] = binding
        # A partial cross-profile publication only refuses the source thread;
        # it never grants the source profile use of the selected owner's facts.
        _store(target_home).update(publish_target)
    return binding
