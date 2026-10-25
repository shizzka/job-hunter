"""Callback prefixes grouped by area, retaining the legacy access-check phases.

Navigation runs before the uncertain-apply check; profile handlers run after it
and own their access checks. Normal handlers run after the router's admin/user
check. Claim and queue policies stay in guards.py and command_queue.py.
"""
from collections.abc import Callable
from dataclasses import dataclass

from telegram_app.callback_handlers import auth, chats, forms, navigation, review


@dataclass(frozen=True)
class CallbackRoute:
    handler: Callable
    phase: str = "normal"
    user_access: str = ""


CALLBACK_ROUTE_GROUPS = {
    'navigation': {
        'sr:': CallbackRoute(navigation.handle_seen_reset, phase='navigation'),
        'hist:': CallbackRoute(navigation.handle_history_scope, phase='navigation'),
        'kbu:save': CallbackRoute(navigation.handle_candidate_upload, phase='navigation'),
        'kbu:cancel': CallbackRoute(navigation.handle_candidate_upload, phase='navigation'),
        'gs:': CallbackRoute(navigation.handle_getting_started, phase='navigation'),
        'dir:': CallbackRoute(navigation.handle_direction, phase='navigation'),
    },
    'review': {
        'stub:': CallbackRoute(review.handle_stub_cover, phase='profile'),
        'rq:': CallbackRoute(review.handle_review_queue, phase='profile'),
        'mad:': CallbackRoute(review.handle_manual_direction, phase='profile'),
        'mar:': CallbackRoute(review.handle_manual_recovery),
        'manual_apply:': CallbackRoute(review.handle_manual_apply, user_access='manual'),
        'manual_fb:': CallbackRoute(review.handle_manual_feedback, user_access='manual'),
        'manual_why:': CallbackRoute(review.handle_manual_why, user_access='manual'),
        'manual_snooze:': CallbackRoute(review.handle_manual_snooze, user_access='manual'),
        'manual_block_company:': CallbackRoute(review.handle_manual_block_company, user_access='manual'),
    },
    'chats': {
        'cbo:': CallbackRoute(chats.handle_chat_owner, phase='profile'),
        'cbs:': CallbackRoute(chats.handle_chat_owner, phase='profile'),
        'cop:': CallbackRoute(chats.handle_chat_owner_profile, phase='profile'),
        'qm:': CallbackRoute(chats.handle_employer_question, phase='profile'),
        'chat_ai_any:': CallbackRoute(chats.handle_chat_ai_manual_reply, user_access='ai'),
        'chat_ai_any_alt:': CallbackRoute(chats.handle_chat_ai_manual_alternative, user_access='ai'),
        'chat_send_any:': CallbackRoute(chats.handle_chat_ai_manual_send, user_access='ai'),
        'chat_ai:': CallbackRoute(chats.handle_chat_ai_reply, user_access='ai'),
        'chat_ai_alt:': CallbackRoute(chats.handle_chat_ai_alternative, user_access='ai'),
        'chat_send:': CallbackRoute(chats.handle_chat_ai_send, user_access='ai'),
    },
    'forms': {
        'gf:': CallbackRoute(forms.handle_form, user_access='ai'),
        'gform_preview:': CallbackRoute(forms.handle_google_form_preview, user_access='ai'),
        'gform_submit:': CallbackRoute(forms.handle_google_form_submit, user_access='ai'),
    },
    'auth': {
        'hh_reauth:': CallbackRoute(auth.handle_hh_reauth, phase='profile'),
        'captcha_retry:': CallbackRoute(auth.handle_captcha_retry, phase='profile'),
        'ca:': CallbackRoute(auth.handle_client_action),
        'cr:': CallbackRoute(auth.handle_client_action),
        'ch:': CallbackRoute(auth.handle_client_action),
        '*': CallbackRoute(auth.handle_client_action),
    },
}

CALLBACK_ROUTES = {prefix: route for routes in CALLBACK_ROUTE_GROUPS.values() for prefix, route in routes.items()}


def route_for_callback(data: str) -> CallbackRoute:
    raw = str(data)
    # Upload actions are exact payloads; every other entry includes its colon.
    route = CALLBACK_ROUTES.get(raw)
    if route is None:
        prefix, separator, _ = raw.partition(":")
        route = CALLBACK_ROUTES.get(prefix + separator)
    # The legacy client parser also accepts whitespace around a client action.
    return route or CALLBACK_ROUTES["*"]
