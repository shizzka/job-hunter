"""Action classification, independent of HH's author classifier and any LLM."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from google_form_filler import extract_google_form_urls
from state_store.chat_responder import get_google_form_previews, google_form_seen_key


def external_invitation(message):
    text = message.get("text") or ""
    urls = [link.get("href") or "" for link in message.get("links") or []]
    urls += re.findall(r"(?:https?://|tg://|(?:www\.)?(?:t\.me|telegram\.me)/)[^\s<>\"']+", text, re.I)
    google_urls = extract_google_form_urls(text, message.get("links") or [])
    found = []
    kind = "внешний этап"
    for raw in urls:
        url = raw.rstrip(".,;!?)»]")
        if re.match(r"^(?:www\.)?(?:t\.me|telegram\.me)/", url, re.I):
            url = "https://" + url
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower()
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https", "tg"}:
            continue
        telegram = host in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"} or parsed.scheme == "tg"
        if url in google_urls or host in {"forms.gle", "docs.google.com"}:
            continue
        if host == "hh.ru" or host.endswith(".hh.ru"):
            continue
        explicit_test = re.search(r"тест|анкет|опрос|задани|assessment|quiz|survey|\bform", text + " " + url, re.I)
        if telegram or explicit_test:
            if url not in found:
                found.append(url)
            if telegram:
                kind = "Telegram-бот / внешний этап"
            elif explicit_test:
                kind = "тест / анкета / опрос"
    if found:
        # Keep the visible task name when present; links themselves stay in buttons.
        description = re.sub(r"(?:https?://|tg://|(?:www\.)?(?:t\.me|telegram\.me)/)\S+", "", text, flags=re.I)
        task = re.search(r"\b((?:тест\w*|анкет\w*|опрос\w*|задани\w*)[^\n:!?]{0,80})", description, re.I)
        if task:
            kind = task.group(1).strip(" .,;—-")
        return {"urls": found, "kind": kind}
    return {}


def question_reason(message):
    text = (message.get("text") or "").strip()
    if "?" in text or "？" in text:
        return "question_punctuation"
    if re.search(r"\b(подскаж\w*|уточни\w*|расскаж\w*|пришл\w*|пришлите|отправ\w*|"
                 r"напиш\w*|сообщ\w*|ответ\w*|како[йев]\w*|когда|сколько|где|почему|"
                 r"готов\w*|смож\w*|рассматрива\w*|интерес\w*|опыт\w*|ожидани\w*)\b", text, re.I):
        return "question_or_request_keywords"
    if message.get("controls"):
        return "interactive_request"
    return "ambiguous_incoming_requires_review"


def decide_chat(messages, chat_state, *, dry_run, max_replies):
    """Never resurrect an incoming question preceding our last outgoing message.

    Keep the latest DOM message as the target, matching existing revision guards.
    Only exact acknowledgements/system notices are suppressed; ambiguity goes to HR review.
    """
    if not messages:
        return "unknown", "no_visible_messages", None
    last_outgoing = max((i for i, msg in enumerate(messages) if msg.get("is_me")), default=-1)
    pending = messages[last_outgoing + 1:]
    if not pending:
        return "ours_last", "outgoing_after_all_visible_incoming", messages[-1]
    last = pending[-1]
    mid = str(last.get("id") or "")
    if not mid:
        return "unknown", "missing_message_id", last
    if chat_state.get("last_replied_msg_id") == mid:
        return "already_handled", "already_replied", last
    if dry_run and chat_state.get("last_previewed_msg_id") == mid:
        return "already_handled", "already_previewed", last
    forms = extract_google_form_urls(last.get("text") or "", last.get("links") or [])
    if forms:
        seen = get_google_form_previews(chat_state)
        if all(google_form_seen_key(url, mid) in seen for url in forms):
            return "already_handled", "google_form_already_observed", last
        return "google_form", "existing_form_preview", last
    if int(chat_state.get("replies_count", 0)) >= max_replies:
        return "limit", "max_replies_per_chat", last
    if last.get("is_ai"):
        return "ai_hh_autoreply", "existing_hh_ai_marker", last
    if external_invitation(last):
        return "external_invite", "external_stage_link", last
    text = (last.get("text") or "").strip()
    if last.get("is_system") or re.fullmatch(
        r"(?:Вы откликнулись на вакансию|Работодатель просмотрел ваше резюме)[.!\s]*", text, re.I
    ):
        return "system_notice", "explicit_system_notice", last
    if re.fullmatch(r"(?:спасибо|благодарю|ок|окей|хорошо|понятно|добрый день|здравствуйте)[!.\s]*", text, re.I):
        return "no_question", "plain_acknowledgement_or_greeting", last
    if not text and not last.get("links") and not last.get("controls"):
        return "unknown", "empty_visible_incoming", last
    return "hr_question", question_reason(last), last
