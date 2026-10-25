"""Command routes after reply-keyboard normalization in telegram_bot_ui.

This is the routing source of truth. Groups describe user-facing areas, not
permissions or menu ownership: existing access checks and reply-menu layouts
retain those responsibilities. Handler bodies remain on TelegramBot for step B.

Phases retain the legacy order: early/impersonation, conflict guard, public,
admin guard, direction prefix/active direction, menu ownership, normal handler.
The direction wildcard deliberately accepts unknown suffixes, as before.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandRoute:
    handler: str
    args: tuple = ()
    phase: str = "normal"
    direction_command: str = ""


ROUTE_GROUPS = {
    'search': {
        '/market_research': CommandRoute('_route_market_research'),
        '/search': CommandRoute('_route_cli', args=('--search', 'search', 3600)),
        '/dryrun': CommandRoute('_route_cli', args=('--dry-run', 'dry-run', 3600)),
    },
    'review queue': {
        '/review': CommandRoute('_route_review'),
    },
    'responses/hh': {
        '/forms': CommandRoute('_route_forms'),
        '/menu_responses': CommandRoute('_route_menu', args=('review',)),
        '/hh_response_count': CommandRoute('_route_hh_responses'),
        '/hh_responses': CommandRoute('_route_hh_responses'),
        '/ai_chat': CommandRoute('_route_chat_ai'),
        '/chat_ai': CommandRoute('_route_chat_ai'),
        '/chat_answer': CommandRoute('_route_chat_ai'),
        '/check': CommandRoute('_route_cli', args=('--check', 'check invitations', 1800)),
    },
    'directions/search settings': {
        '/direction*': CommandRoute('_route_direction', phase='direction'),
        '/companies': CommandRoute('_route_companies'),
        '/company_block': CommandRoute('_route_companies'),
        '/company_unblock': CommandRoute('_route_companies'),
        '/search_settings': CommandRoute('_route_search_settings'),
        '/search_conditions': CommandRoute('_route_search_conditions'),
        '/search_experience': CommandRoute('_route_search_experience'),
        '/search_experience_any': CommandRoute('_route_experience', args=('', 'любой')),
        '/search_experience_none': CommandRoute('_route_experience', args=('noExperience', 'без опыта')),
        '/search_experience_1_3': CommandRoute('_route_experience', args=('between1And3', '1–3 года')),
        '/search_experience_3_6': CommandRoute('_route_experience', args=('between3And6', '3–6 лет')),
        '/search_experience_6_plus': CommandRoute('_route_experience', args=('moreThan6', '6+ лет')),
        '/search_salary': CommandRoute('_route_search_salary'),
        '/search_salary_toggle': CommandRoute('_route_search_salary_toggle'),
        '/search_remote_toggle': CommandRoute('_route_search_remote_toggle'),
        '/search_application_mode': CommandRoute('_route_search_application_mode'),
        '/search_application_preview': CommandRoute('_route_application_mode', args=('preview',), direction_command='/search_application_preview'),
        '/search_application_confirm': CommandRoute('_route_application_mode', args=('confirm',), direction_command='/search_application_confirm'),
        '/search_application_auto': CommandRoute('_route_application_mode', args=('auto',), direction_command='/search_application_auto'),
        '/search_edit': CommandRoute('_route_search_edit', direction_command='/search_edit'),
        '/search_suggest': CommandRoute('_route_search_suggest', direction_command='/search_suggest'),
        '/search_use_draft': CommandRoute('_route_search_use_draft', direction_command='/search_use_draft'),
    },
    'hh & resumes': {
        '/menu_hh': CommandRoute('_route_menu', args=('run',)),
        '/menu_run': CommandRoute('_route_menu', args=('run',)),
        '/search_resume': CommandRoute('_route_search_resume', direction_command='/search_resume'),
        '/hh_auth': CommandRoute('_route_hh_auth'),
        '/hh_resumes': CommandRoute('_route_hh_resumes'),
        '/analyze': CommandRoute('_route_cli', args=('--analyze-resume', 'analyze resume', 3600)),
        '/grabresume': CommandRoute('_route_cli', args=('--grab-resume', 'grab resume', 1800), direction_command='/direction_refresh'),
        '/refresh_resume': CommandRoute('_route_cli', args=('--refresh-resume', 'refresh resume', 1800), direction_command='/direction_refresh'),
    },
    'candidate facts': {
        '/candidate_upload': CommandRoute('_route_candidate_upload'),
        '/candidate_upload_cancel': CommandRoute('_route_candidate_upload'),
        '/candidate_upload_save': CommandRoute('_route_candidate_upload'),
        '/candidate_add_fact': CommandRoute('_route_candidate'),
        '/candidate_edit': CommandRoute('_route_candidate'),
        '/candidate_facts': CommandRoute('_route_candidate'),
        '/candidate_interview': CommandRoute('_route_candidate'),
        '/candidate_profile': CommandRoute('_route_candidate'),
        '/candidate_save': CommandRoute('_route_candidate'),
        '/candidate_skip': CommandRoute('_route_candidate'),
    },
    'stats/logs/monitoring': {
        '/menu_monitor': CommandRoute('_route_menu', args=('monitor',)),
        '/menu_logs': CommandRoute('_route_menu', args=('logs',)),
        '/diagnostics': CommandRoute('_route_diagnostics'),
        '/status': CommandRoute('_route_status'),
        '/stats': CommandRoute('_route_history'),
        '/digest': CommandRoute('_route_history'),
        '/research': CommandRoute('_route_history'),
        '/runs': CommandRoute('_route_history'),
        '/log': CommandRoute('_route_history'),
        '/hunter_log': CommandRoute('_route_history'),
        '/chat_log': CommandRoute('_route_history'),
        '/hunter_chat_log': CommandRoute('_route_history'),
        '/why_no_apply': CommandRoute('_route_why_no_apply'),
        '/raw_log': CommandRoute('_route_raw_log'),
        '/backfill': CommandRoute('_route_cli', args=('--analytics-backfill', 'analytics backfill', 3600)),
    },
    'schedule': {
        '/menu_repeat': CommandRoute('_route_menu', args=('repeat',)),
        '/schedule': CommandRoute('_route_schedule'),
        '/daemon_on': CommandRoute('_route_daemon_on'),
        '/repeat_3day': CommandRoute('_route_repeat', args=('3 раза в день', 480)),
        '/repeat_daily': CommandRoute('_route_repeat', args=('1 раз в день', 1440)),
        '/repeat_weekly': CommandRoute('_route_repeat', args=('1 раз в неделю', 10080)),
        '/repeat_off': CommandRoute('_route_repeat_off'),
        '/daemon_off': CommandRoute('_route_daemon_off'),
    },
    'admin': {
        '/act_as': CommandRoute('_route_impersonation', phase='impersonation'),
        '/act_as_self': CommandRoute('_route_impersonation', phase='impersonation'),
        '/cancel_active': CommandRoute('_route_cancel_active', phase='early'),
        '/llm_balance': CommandRoute('_route_llm'),
        '/llm_test': CommandRoute('_route_llm'),
        '/menu_llm': CommandRoute('_route_llm'),
        '/menu_admin': CommandRoute('_route_menu', args=('admin',)),
        '/profiles': CommandRoute('_route_profiles'),
        '/profile': CommandRoute('_route_profile'),
        '/users': CommandRoute('_route_users'),
        '/client_status': CommandRoute('_route_client_status'),
        '/clients': CommandRoute('_route_clients'),
        '/client_hh_auth': CommandRoute('_route_client_hh_auth'),
        '/client_approve': CommandRoute('_route_client_approve'),
        '/client_reject': CommandRoute('_route_client_reject'),
        '/ai_limits': CommandRoute('_route_ai_limits'),
        '/ai_grant': CommandRoute('_route_ai_grant'),
        '/ai_reset': CommandRoute('_route_ai_reset'),
        '/grant': CommandRoute('_route_grant'),
        '/revoke': CommandRoute('_route_revoke'),
        '/seen_reset': CommandRoute('_route_seen_reset'),
    },
    'navigation': {
        '/busy': CommandRoute('_route_busy', phase='early'),
        '/back': CommandRoute('_route_back', phase='early'),
        '/help': CommandRoute('_route_main', phase='public'),
        '/menu': CommandRoute('_route_main', phase='public'),
        '/start': CommandRoute('_route_main', phase='public'),
        '/getting_started': CommandRoute('_route_getting_started', phase='public'),
    },
}

# One lookup index, derived from the grouped data (no second command list).
COMMAND_ROUTES = {command: route for routes in ROUTE_GROUPS.values() for command, route in routes.items()}


def route_for_command(command: str) -> CommandRoute | None:
    route = COMMAND_ROUTES.get(command)
    if route is None and command.startswith("/direction"):
        route = COMMAND_ROUTES["/direction*"]
    return route
