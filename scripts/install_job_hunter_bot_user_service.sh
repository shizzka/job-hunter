#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
UNIT_NAME="job-hunter-bot.service"
TEMPLATE_PATH="${PROJECT_ROOT}/deploy/systemd/user/${UNIT_NAME}.in"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
TARGET_PATH="${SYSTEMD_USER_DIR}/${UNIT_NAME}"
ENABLE_NOW=0

usage() {
    cat <<'EOF'
Usage: ./scripts/install_job_hunter_bot_user_service.sh [--enable-now]

Installs a user-level systemd unit for the Job Hunter Telegram bot.

Options:
  --enable-now   Install, daemon-reload, enable, and start the service immediately
  -h, --help     Show this help message
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --enable-now)
            ENABLE_NOW=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [ ! -f "$TEMPLATE_PATH" ]; then
    echo "Template not found: $TEMPLATE_PATH" >&2
    exit 1
fi

mkdir -p "$SYSTEMD_USER_DIR"

escaped_root="$(printf '%s' "$PROJECT_ROOT" | sed 's/[|&\\]/\\&/g')"
tmp_unit="$(mktemp)"
trap 'rm -f "$tmp_unit"' EXIT

sed "s|__PROJECT_ROOT__|${escaped_root}|g" "$TEMPLATE_PATH" > "$tmp_unit"
install -m 0644 "$tmp_unit" "$TARGET_PATH"

systemctl --user daemon-reload

echo "Installed user unit:"
echo "  $TARGET_PATH"

if [ "$ENABLE_NOW" -eq 1 ]; then
    systemctl --user enable --now "$UNIT_NAME"
    echo
    echo "Service enabled and started:"
    echo "  systemctl --user status ${UNIT_NAME}"
else
    echo
    echo "Next steps:"
    echo "  systemctl --user enable --now ${UNIT_NAME}"
fi

echo
echo "Useful commands:"
echo "  systemctl --user status ${UNIT_NAME}"
echo "  journalctl --user -u ${UNIT_NAME} -f"
echo "  systemctl --user restart ${UNIT_NAME}"
echo "  systemctl --user stop ${UNIT_NAME}"
echo
echo "For startup after reboot without logging into the desktop session:"
echo "  sudo loginctl enable-linger ${USER}"
