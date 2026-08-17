#!/usr/bin/env bash
set -euo pipefail

# Job Hunter — скрипт запуска
# Использование:
#   ./run.sh login       — ручной логин
#   ./run.sh google-login — ручной логин в Google для Google Forms
#   ./run.sh geekjob-login — ручной логин в GeekJob
#   ./run.sh search      — один прогон
#   ./run.sh check       — проверка приглашений
#   ./run.sh daemon      — демон (в фоне)
#   ./run.sh bot         — Telegram bot (foreground)
#   ./run.sh bot-daemon  — Telegram bot (в фоне)
#   ./run.sh stats       — статистика
#   ./run.sh analytics [days] — аналитика за N дней
#   ./run.sh filter-audit [days] — replay-аудит фильтров по analytics
#   ./run.sh analytics-backfill — подтянуть историю в аналитику
#   ./run.sh retry-preview — показать HH retry-кандидатов без откликов
#   ./run.sh retry-block-company "Компания" — не слать retry в компанию
#   ./run.sh retry-blocked-companies — список company blocklist для retry
#   ./run.sh resume-status — проверить кнопку "поднять резюме" на HH без клика
#   ./run.sh resume-boost ПОДНЯТЬ — вручную нажать "поднять резюме" (требует HH_RESUME_BOOST_ENABLED=1)
#   ./run.sh google-form-preview <chat_id> [message_id] — подготовить Google Form из HH-чата
#   ./run.sh google-form-submit <token> — отправить подготовленную Google Form
#   ./run.sh dry-run     — поиск без откликов

cd "$(dirname "$0")"
VENV="${JOB_HUNTER_PYTHON:-./venv/bin/python}"
if [ ! -x "$VENV" ]; then
    VENV="${JOB_HUNTER_PYTHON:-python3}"
fi
ENV_FILE="${JOB_HUNTER_ENV_FILE:-$HOME/.job-hunter/job-hunter.env}"

# Explicit per-run overrides must win over values loaded from the env file.
HH_CHAT_AUTOSEND_OVERRIDE_SET=0
HH_CHAT_AUTOSEND_OVERRIDE=""
if [ "${HH_CHAT_AUTOSEND+x}" = "x" ]; then
    HH_CHAT_AUTOSEND_OVERRIDE_SET=1
    HH_CHAT_AUTOSEND_OVERRIDE="$HH_CHAT_AUTOSEND"
fi

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi
if [ "$HH_CHAT_AUTOSEND_OVERRIDE_SET" -eq 1 ]; then
    export HH_CHAT_AUTOSEND="$HH_CHAT_AUTOSEND_OVERRIDE"
fi

# Поддержка --profile <name>: ./run.sh --profile alice search
PROFILE_ARG=""
PROFILE_EXPLICIT=0
if [ "${1:-}" = "--profile" ]; then
    PROFILE_ARG="--profile ${2:?Profile name required}"
    PROFILE_EXPLICIT=1
    shift 2
fi

MODE="${1:-search}"
DEFAULT_PROFILE="${JOB_HUNTER_DEFAULT_PROFILE:-}"

if [ "$PROFILE_EXPLICIT" -eq 0 ] && [ -n "$DEFAULT_PROFILE" ]; then
    case "$MODE" in
        profiles|list-profiles|create-profile|setup)
            ;;
        *)
            PROFILE_ARG="--profile $DEFAULT_PROFILE"
            ;;
    esac
fi

case "$MODE" in
    login)
        $VENV agent.py $PROFILE_ARG --login
        ;;
    google-login)
        $VENV agent.py $PROFILE_ARG --google-login
        ;;
    superjob-login)
        $VENV agent.py $PROFILE_ARG --superjob-login
        ;;
    habr-login)
        $VENV agent.py $PROFILE_ARG --habr-login
        ;;
    geekjob-login)
        $VENV agent.py $PROFILE_ARG --geekjob-login
        ;;
    search)
        $VENV agent.py $PROFILE_ARG --search
        ;;
    fresh-search|fresh)
        $VENV agent.py $PROFILE_ARG --fresh-search
        ;;
    check)
        $VENV agent.py $PROFILE_ARG --check
        ;;
    daemon)
        $VENV job_hunter_ctl.py $PROFILE_ARG daemon-start
        ;;
    bot)
        $VENV telegram_bot.py $PROFILE_ARG
        ;;
    bot-daemon|botd)
        $VENV job_hunter_ctl.py $PROFILE_ARG bot-start
        ;;
    stats)
        $VENV agent.py $PROFILE_ARG --stats
        ;;
    analytics|funnel)
        DAYS="${2:-}"
        if [ -n "$DAYS" ]; then
            $VENV agent.py $PROFILE_ARG --analytics-report "$DAYS"
        else
            $VENV agent.py $PROFILE_ARG --analytics-report
        fi
        ;;
    digest)
        $VENV agent.py $PROFILE_ARG --digest
        ;;
    analytics-backfill|backfill)
        $VENV agent.py $PROFILE_ARG --analytics-backfill
        ;;
    filter-audit|audit-filters)
        DAYS="${2:-}"
        if [ -n "$DAYS" ]; then
            $VENV agent.py $PROFILE_ARG --filter-audit "$DAYS"
        else
            $VENV agent.py $PROFILE_ARG --filter-audit
        fi
        ;;
    retry-preview|preview-retry)
        $VENV agent.py $PROFILE_ARG --hh-retry-preview
        ;;
    retry-block-company|block-company)
        COMPANY="${*:2}"
        if [ -z "$COMPANY" ]; then
            echo 'Укажи компанию: ./run.sh retry-block-company "Company Name"'
            exit 1
        fi
        $VENV agent.py $PROFILE_ARG --hh-retry-block-company "$COMPANY"
        ;;
    retry-unblock-company|unblock-company)
        COMPANY="${*:2}"
        if [ -z "$COMPANY" ]; then
            echo 'Укажи компанию: ./run.sh retry-unblock-company "Company Name"'
            exit 1
        fi
        $VENV agent.py $PROFILE_ARG --hh-retry-unblock-company "$COMPANY"
        ;;
    retry-blocked-companies|blocked-companies)
        $VENV agent.py $PROFILE_ARG --hh-retry-list-blocked-companies
        ;;
    hh-resume-status|resume-status|boost-status)
        $VENV agent.py $PROFILE_ARG --hh-resume-boost-status
        ;;
    hh-resume-boost|resume-boost)
        CONFIRM="${2:-}"
        $VENV agent.py $PROFILE_ARG --hh-resume-boost --hh-resume-boost-confirm "$CONFIRM"
        ;;
    google-form-preview|gform-preview)
        CHAT_ID="${2:?Укажи chat_id: ./run.sh google-form-preview <chat_id> [message_id]}"
        MSG_ID="${3:-}"
        if [ -n "$MSG_ID" ]; then
            $VENV agent.py $PROFILE_ARG --google-form-preview "$CHAT_ID" --chat-message-id "$MSG_ID"
        else
            $VENV agent.py $PROFILE_ARG --google-form-preview "$CHAT_ID"
        fi
        ;;
    google-form-submit|gform-submit)
        TOKEN="${2:?Укажи token: ./run.sh google-form-submit <token>}"
        $VENV agent.py $PROFILE_ARG --google-form-submit "$TOKEN"
        ;;
    dry-run|dryrun)
        $VENV agent.py $PROFILE_ARG --dry-run
        ;;
    superjob-dry-run|superjob-dryrun)
        $VENV agent.py $PROFILE_ARG --source superjob --dry-run
        ;;
    superjob-search|superjob)
        $VENV agent.py $PROFILE_ARG --source superjob --search
        ;;
    habr-dry-run|habr-dryrun)
        $VENV agent.py $PROFILE_ARG --source habr --dry-run
        ;;
    habr-search|habr)
        $VENV agent.py $PROFILE_ARG --source habr --search
        ;;
    geekjob-dry-run|geekjob-dryrun)
        $VENV agent.py $PROFILE_ARG --source geekjob --dry-run
        ;;
    geekjob-search|geekjob)
        $VENV agent.py $PROFILE_ARG --source geekjob --search
        ;;
    grab-resume|resume)
        $VENV agent.py $PROFILE_ARG --grab-resume
        ;;
    analyze-resume|analyze)
        $VENV agent.py $PROFILE_ARG --analyze-resume
        ;;
    extract-facts|facts)
        $VENV agent.py $PROFILE_ARG --extract-facts
        ;;
    chat-respond|chats|chat)
        $VENV agent.py $PROFILE_ARG --chat-respond
        ;;
    chat-respond-one|chat-one)
        CHAT_ID="${2:?Укажи chat_id: ./run.sh chat-respond-one <chat_id> [message_id]}"
        MSG_ID="${3:-}"
        if [ -n "$MSG_ID" ]; then
            $VENV agent.py $PROFILE_ARG --chat-respond-one "$CHAT_ID" --chat-message-id "$MSG_ID" --chat-allow-suspicious
        else
            $VENV agent.py $PROFILE_ARG --chat-respond-one "$CHAT_ID" --chat-allow-suspicious
        fi
        ;;
    profiles|list-profiles)
        $VENV agent.py --list-profiles
        ;;
    create-profile)
        NAME="${2:?Укажи имя профиля: ./run.sh create-profile <name>}"
        $VENV agent.py --create-profile "$NAME"
        ;;
    setup)
        $VENV setup_profile.py
        ;;
    status)
        $VENV job_hunter_ctl.py $PROFILE_ARG status
        ;;
    bot-status)
        $VENV job_hunter_ctl.py $PROFILE_ARG bot-status
        ;;
    bot-stop)
        $VENV job_hunter_ctl.py $PROFILE_ARG bot-stop
        ;;
    stop)
        $VENV job_hunter_ctl.py $PROFILE_ARG daemon-stop
        ;;
    *)
        echo "Usage: $0 [--profile <name>] {login|google-login|search|fresh-search|check|daemon|bot|bot-daemon|status|bot-status|stats|analytics|filter-audit|digest|dry-run|grab-resume|resume-status|resume-boost|google-form-preview|google-form-submit|chat-respond|chat-respond-one|retry-preview|retry-block-company|retry-unblock-company|retry-blocked-companies|create-profile|profiles|bot-stop|stop}"
        exit 1
        ;;
esac
