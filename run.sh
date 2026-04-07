#!/usr/bin/env bash
set -euo pipefail

# Job Hunter — скрипт запуска
# Использование:
#   ./run.sh login       — ручной логин
#   ./run.sh geekjob-login — ручной логин в GeekJob
#   ./run.sh search      — один прогон
#   ./run.sh check       — проверка приглашений
#   ./run.sh daemon      — демон (в фоне)
#   ./run.sh bot         — Telegram bot (foreground)
#   ./run.sh bot-daemon  — Telegram bot (в фоне)
#   ./run.sh stats       — статистика
#   ./run.sh analytics-backfill — подтянуть историю в аналитику
#   ./run.sh dry-run     — поиск без откликов

cd "$(dirname "$0")"
VENV="${JOB_HUNTER_PYTHON:-./venv/bin/python}"
if [ ! -x "$VENV" ]; then
    VENV="${JOB_HUNTER_PYTHON:-python3}"
fi
ENV_FILE="${JOB_HUNTER_ENV_FILE:-$HOME/.job-hunter/job-hunter.env}"

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
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
    digest)
        $VENV agent.py $PROFILE_ARG --digest
        ;;
    analytics-backfill|backfill)
        $VENV agent.py $PROFILE_ARG --analytics-backfill
        ;;
    dry-run|dryrun)
        $VENV agent.py $PROFILE_ARG --dry-run
        ;;
    superjob-dry-run|superjob-dryrun)
        HH_ENABLED=0 SUPERJOB_ENABLED=1 HABR_ENABLED=0 GEEKJOB_ENABLED=0 $VENV agent.py $PROFILE_ARG --dry-run
        ;;
    superjob-search|superjob)
        HH_ENABLED=0 SUPERJOB_ENABLED=1 HABR_ENABLED=0 GEEKJOB_ENABLED=0 $VENV agent.py $PROFILE_ARG --search
        ;;
    habr-dry-run|habr-dryrun)
        HH_ENABLED=0 SUPERJOB_ENABLED=0 HABR_ENABLED=1 GEEKJOB_ENABLED=0 $VENV agent.py $PROFILE_ARG --dry-run
        ;;
    habr-search|habr)
        HH_ENABLED=0 SUPERJOB_ENABLED=0 HABR_ENABLED=1 GEEKJOB_ENABLED=0 $VENV agent.py $PROFILE_ARG --search
        ;;
    geekjob-dry-run|geekjob-dryrun)
        HH_ENABLED=0 SUPERJOB_ENABLED=0 HABR_ENABLED=0 GEEKJOB_ENABLED=1 $VENV agent.py $PROFILE_ARG --dry-run
        ;;
    geekjob-search|geekjob)
        HH_ENABLED=0 SUPERJOB_ENABLED=0 HABR_ENABLED=0 GEEKJOB_ENABLED=1 $VENV agent.py $PROFILE_ARG --search
        ;;
    grab-resume|resume)
        $VENV agent.py $PROFILE_ARG --grab-resume
        ;;
    analyze-resume|analyze)
        $VENV agent.py $PROFILE_ARG --analyze-resume
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
        echo "Usage: $0 [--profile <name>] {login|search|check|daemon|bot|bot-daemon|status|bot-status|stats|digest|dry-run|grab-resume|create-profile|profiles|bot-stop|stop}"
        exit 1
        ;;
esac
