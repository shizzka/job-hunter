#!/usr/bin/env bash
set -euo pipefail

# Job Hunter — скрипт запуска
# Использование:
#   ./run.sh login       — ручной логин
#   ./run.sh geekjob-login — ручной логин в GeekJob
#   ./run.sh search      — один прогон
#   ./run.sh check       — проверка приглашений
#   ./run.sh daemon      — демон (в фоне)
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

MODE="${1:-search}"

case "$MODE" in
    login)
        $VENV agent.py --login
        ;;
    superjob-login)
        $VENV agent.py --superjob-login
        ;;
    habr-login)
        $VENV agent.py --habr-login
        ;;
    geekjob-login)
        $VENV agent.py --geekjob-login
        ;;
    search)
        $VENV agent.py --search
        ;;
    check)
        $VENV agent.py --check
        ;;
    daemon)
        echo "Starting Job Hunter daemon..."
        nohup $VENV agent.py --daemon >> /tmp/job-hunter.log 2>&1 &
        echo "PID: $!"
        echo "Log: /tmp/job-hunter.log"
        ;;
    stats)
        $VENV agent.py --stats
        ;;
    analytics-backfill|backfill)
        $VENV agent.py --analytics-backfill
        ;;
    dry-run|dryrun)
        $VENV agent.py --dry-run
        ;;
    superjob-dry-run|superjob-dryrun)
        HH_ENABLED=0 SUPERJOB_ENABLED=1 HABR_ENABLED=0 GEEKJOB_ENABLED=0 $VENV agent.py --dry-run
        ;;
    superjob-search|superjob)
        HH_ENABLED=0 SUPERJOB_ENABLED=1 HABR_ENABLED=0 GEEKJOB_ENABLED=0 $VENV agent.py --search
        ;;
    habr-dry-run|habr-dryrun)
        HH_ENABLED=0 SUPERJOB_ENABLED=0 HABR_ENABLED=1 GEEKJOB_ENABLED=0 $VENV agent.py --dry-run
        ;;
    habr-search|habr)
        HH_ENABLED=0 SUPERJOB_ENABLED=0 HABR_ENABLED=1 GEEKJOB_ENABLED=0 $VENV agent.py --search
        ;;
    geekjob-dry-run|geekjob-dryrun)
        HH_ENABLED=0 SUPERJOB_ENABLED=0 HABR_ENABLED=0 GEEKJOB_ENABLED=1 $VENV agent.py --dry-run
        ;;
    geekjob-search|geekjob)
        HH_ENABLED=0 SUPERJOB_ENABLED=0 HABR_ENABLED=0 GEEKJOB_ENABLED=1 $VENV agent.py --search
        ;;
    grab-resume|resume)
        $VENV agent.py --grab-resume
        ;;
    stop)
        pkill -f "agent.py --daemon" && echo "Stopped" || echo "Not running"
        ;;
    *)
        echo "Usage: $0 {login|superjob-login|habr-login|geekjob-login|search|check|daemon|stats|analytics-backfill|dry-run|superjob-dry-run|superjob-search|habr-dry-run|habr-search|geekjob-dry-run|geekjob-search|grab-resume|stop}"
        exit 1
        ;;
esac
